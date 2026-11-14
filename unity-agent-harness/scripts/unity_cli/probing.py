from __future__ import annotations

import hashlib
from dataclasses import replace
import json
import pathlib
import tempfile
import uuid
from typing import Any

from .compile import CompileWorkflow, WorkflowRefusal, native_failure
from .console import ConsoleWorkflow, WorkflowConsoleRefusal
from .lifecycle import SessionRefusal, WorkflowSessionLease
from .reports import ReportPathError, ReportTarget
from .transport import _redact_value


class ProbeRefusal(WorkflowRefusal):
    pass


class ProbeWorkflow:
    def __init__(self, project: str | pathlib.Path, session):
        self.project = pathlib.Path(project).resolve()
        self.session = session

    def run(self, source, entry, completion_text, output, enter_play=False):
        return self._execute(source, output, entry=entry, completion_text=completion_text, enter_play=enter_play)

    def check(self, source, output):
        return self._execute(source, output, dry_run=True)

    def _execute(self, source, output, *, entry=None, completion_text=None, enter_play=False, dry_run=False):
        try:
            target = ReportTarget(self.project, output)
        except ReportPathError as exc:
            raise ProbeRefusal(exc.code, str(exc), exc.details) from exc
        report: dict[str, Any] = {'schemaVersion': 1, 'ok': False, 'action': 'probe-check' if dry_run else 'probe',
                                  'project': str(self.project), 'commands': [], 'restoration': {'ownedPlayTransition': False}}
        deadline = self.session._deadline()
        error = None
        try:
            if not isinstance(enter_play, bool):
                raise ProbeRefusal('INVALID_ARGUMENT', 'enter_play must be Boolean')
            if not dry_run and (not isinstance(entry, str) or not entry.strip() or not isinstance(completion_text, str) or not completion_text.strip()):
                raise ProbeRefusal('INVALID_ARGUMENT', 'Supply a nonempty entry and exact completion message')
            compile_owner = CompileWorkflow(self.project, self.session)
            sources = compile_owner._sources(source)
            if len(sources) != 1 or sources[0].suffix.lower() != '.cs':
                raise ProbeRefusal('INVALID_ARGUMENT', 'Select exactly one C# source file')
            report['sources'] = compile_owner._hash_sources(sources)
            report['entry'] = entry
            run_id = uuid.uuid4().hex
            report['runId'] = run_id
            with tempfile.TemporaryDirectory(prefix='unity-probe-') as temporary:
                staged = pathlib.Path(temporary) / sources[0].name
                raw_source = sources[0].read_bytes()
                if hashlib.sha256(raw_source).hexdigest() != report['sources'][0]['sha256']:
                    raise ProbeRefusal('PROBE_SOURCE_CHANGED', 'Source changed while preparing the probe')
                if not dry_run:
                    marker = completion_text.replace('{run_id}', run_id)
                    report.update(completionText=marker, correlation='unique-run-id' if '{run_id}' in completion_text else 'bounded-current-sequence')
                    if '{run_id}' in completion_text:
                        encoded = completion_text.encode('utf-8')
                        if encoded not in raw_source:
                            raise ProbeRefusal('PROBE_TEMPLATE_MISSING', 'The exact completion template must occur literally in the source')
                        raw_source = raw_source.replace(encoded, marker.encode('utf-8'))
                staged.write_bytes(raw_source)
                report['stagedSha256'] = hashlib.sha256(raw_source).hexdigest()
                with self.session.workflow_session(deadline=deadline) as lease:
                    report['editorIdentity'] = lease.identity.as_dict()
                    # An owned transition needs time to restore after the probe's deadline.
                    reserve = min(5.0, lease.remaining() / 4) if enter_play else 0.0
                    action_lease = replace(lease, deadline=deadline - reserve) if isinstance(lease, WorkflowSessionLease) else lease
                    owned = False
                    primary_error = None
                    try:
                        state = self._state(lease, report)
                        if not dry_run and state['playMode'] == 'paused':
                            raise ProbeRefusal('PROBE_EDITOR_PAUSED', 'The Editor is paused; unpause or stop it first')
                        edit_mode = not dry_run and not enter_play and state['playMode'] == 'stopped'
                        dirty_before = self._dirty(action_lease) if edit_mode else None
                        if not dry_run and enter_play and state['playMode'] == 'stopped':
                            action_lease.verify_current()
                            dirty = self.session.transport.dirty_scenes(action_lease.identity, timeout=action_lease.remaining(), deadline=action_lease.transport_deadline)
                            action_lease.verify_current()
                            if not dirty.success:
                                raise ProbeRefusal(dirty.code or 'DIRTY_STATE_UNKNOWN', dirty.message or 'Dirty-scene state could not be inspected', dirty.diagnostics)
                            if not isinstance(dirty.data, list):
                                raise ProbeRefusal('DIRTY_STATE_INVALID', 'Dirty-scene inspection returned an unexpected result')
                            if dirty.data:
                                raise ProbeRefusal('DIRTY_SCENES', 'Save or discard dirty scenes explicitly before entering play', {'scenes': dirty.data})
                            def own_transition():
                                nonlocal owned
                                owned = True
                                report['restoration']['ownedPlayTransition'] = True
                            self._native(action_lease, report, 'editor.playmode', 'start', before_dispatch=own_transition)
                            self._wait_play(action_lease, report, 'playing')
                        self._verify_sources(compile_owner, sources, report, action_lease)
                        baseline = self._console(action_lease, report)
                        report['baseline'] = {'cursor': baseline['cursor'], 'session': baseline['session']}
                        cli = self.session.transport.ensure_cli(timeout=action_lease.remaining(), deadline=action_lease.transport_deadline)
                        native_file = self.session.platform.cli_project_path(str(staged), cli, timeout=action_lease.remaining(), deadline=action_lease.transport_deadline)
                        arguments = {'file': native_file, 'dry_run': dry_run, 'timeout': max(1, int(action_lease.remaining()))}
                        if not dry_run: arguments['entry'] = entry
                        self._run_script(action_lease, report, arguments, dry_run)
                        cursor = baseline['cursor']
                        while True:
                            self._verify_sources(compile_owner, sources, report, action_lease)
                            parsed = self._console(action_lease, report, cursor, baseline['session'])
                            if any(row['logType'] in {'Error', 'Assert', 'Exception'} for row in parsed['entries']):
                                raise ProbeRefusal('PROBE_RUNTIME_FAILED', 'Current probe console contains Error, Assert, or Exception')
                            ground = parsed['groundTruth']
                            if ground is not None and ground['compilationFailed']:
                                raise ProbeRefusal('PROBE_SCRIPT_FAILED', 'Editor reports compilation failure')
                            if dry_run:
                                report['ok'] = True
                                break
                            matches = [row for row in parsed['entries'] if row['message'] == marker and row['seeded'] is False]
                            if matches:
                                report['completion'] = matches[0]
                                report['ok'] = True
                                break
                            nearest = [row for row in parsed['entries'] if row['seeded'] is False and marker in row['message']]
                            if nearest:
                                raise ProbeRefusal('PROBE_COMPLETION_MISMATCH', 'A current probe message contains the completion text but does not match it exactly', {'completionText': marker, 'nearestLine': nearest[0]})
                            cursor = parsed['cursor']
                            self.session.clock.sleep(min(0.1, action_lease.remaining()))
                        self._state(action_lease, report, expected='playing' if owned else state['playMode'])
                        if edit_mode:
                            report['scenesDirtyBeforeProbe'] = dirty_before
                            changed = [scene for scene in self._dirty(action_lease) if scene not in dirty_before]
                            if changed:
                                raise ProbeRefusal('PROBE_CHANGED_SCENE', 'The edit-mode probe changed open scenes; nothing was reverted or saved', {'scenes': changed})
                        action_lease.verify_current()
                        self._verify_sources(compile_owner, sources, report, action_lease)
                    except BaseException as exc:
                        primary_error = exc
                        raise
                    finally:
                        if owned:
                            try:
                                lease.verify_current()
                                self._native(lease, report, 'editor.playmode', 'stop')
                                self._wait_play(lease, report, 'stopped')
                                report['restoration']['restored'] = True
                            except Exception as exc:
                                restoration_error = exc if isinstance(exc, (WorkflowRefusal, WorkflowConsoleRefusal, SessionRefusal)) else ProbeRefusal('PROBE_RESTORATION_FAILED', 'The owned play transition could not be restored', {'reason': str(exc), 'type': type(exc).__name__})
                                report['restoration'].update(restored=False, error=restoration_error.as_dict()['error'])
                                if primary_error is None:
                                    raise restoration_error
            report['ok'] = True
        except (WorkflowRefusal, WorkflowConsoleRefusal, SessionRefusal) as exc:
            code = 'PROBE_TIMEOUT' if exc.code in {'ACTION_TIMEOUT', 'READY_TIMEOUT'} else exc.code
            error = ProbeRefusal(code, str(exc), exc.details)
        except (OSError, UnicodeError, ValueError) as exc:
            error = ProbeRefusal('PROBE_INPUT_FAILED', 'Probe source or staging could not be read or written', {'reason': str(exc)})
        except Exception as exc:
            error = ProbeRefusal('PROBE_INTERNAL_FAILED', 'Probe could not complete; inspect retained command evidence', {'reason': str(exc), 'type': type(exc).__name__})
        if error is not None:
            report.update(ok=False, error=error.as_dict()['error'])
        try:
            payload = json.dumps(_redact_value(report), sort_keys=True, separators=(',', ':')).encode('utf-8')
            target.publish(payload)
        except ReportPathError as exc:
            raise ProbeRefusal(exc.code, str(exc), exc.details) from exc
        finally:
            target.close()
        artifact = {'artifactPath': str(target.path), 'artifactSha256': hashlib.sha256(payload).hexdigest()}
        if error is not None:
            error.details.update(artifact, artifactPreserved=True)
            raise error
        return {**{key: value for key, value in report.items() if key != 'commands'}, **artifact}

    def _native(self, lease, report, group, name, params=None, *, before_dispatch=None, human=False, allow_failure=False):
        """Run one native call under the lease and log it; a failed call refuses by its exit code unless the caller reads the failure itself."""
        lease.verify_current()
        timeout = lease.remaining()
        if before_dispatch is not None:
            before_dispatch()
        result = self.session.transport.native(group, name, params, project=str(self.project), pid=lease.identity.pid,
                                               timeout=timeout, deadline=lease.transport_deadline, human=human)
        report['commands'].append({'command': f'{group} {name}', 'arguments': params, **result.evidence()})
        lease.verify_current()
        if not result.ok and not allow_failure:
            failure = native_failure(result, f'{group} {name}')
            raise ProbeRefusal(failure.code, str(failure), failure.details)
        return result

    def _dirty(self, lease):
        """The open scenes with unsaved changes, as they were when asked."""
        lease.verify_current()
        dirty = self.session.transport.dirty_scenes(lease.identity, timeout=lease.remaining(), deadline=lease.transport_deadline)
        lease.verify_current()
        if not dirty.success:
            raise ProbeRefusal(dirty.code or 'DIRTY_STATE_UNKNOWN', dirty.message or 'Dirty-scene state could not be inspected', dirty.diagnostics)
        if not isinstance(dirty.data, list):
            raise ProbeRefusal('DIRTY_STATE_INVALID', 'Dirty-scene inspection returned an unexpected result')
        return dirty.data

    def _console(self, lease, report, since=None, source_session=None):
        arguments = {'tail': ConsoleWorkflow.CAPTURE_LIMIT, 'level': 'log'}
        if since is not None: arguments.update(since=since, since_session=source_session)
        payload = self._native(lease, report, 'console', 'get', arguments).value
        parsed = ConsoleWorkflow._validate_payload(payload, None, since, source_session)
        if parsed['dropped'] or parsed['reset']:
            raise ProbeRefusal('PROBE_CONSOLE_GAP', 'Console history was reset or dropped; probe evidence is incomplete')
        return parsed

    def _state(self, lease, report, expected=None):
        state = self._native(lease, report, 'editor', 'status').value
        if not isinstance(state, dict) or not isinstance(state.get('projectPath'), str):
            raise ProbeRefusal('EDITOR_STATUS_INVALID', 'Editor status must identify its project')
        project = self.session.platform.canonicalize_external_project(state['projectPath'], deadline=lease.transport_deadline)
        if project != lease.identity.project:
            raise ProbeRefusal('PROJECT_IDENTITY_MISMATCH', 'Editor status project differs from the leased project')
        if state.get('status') not in {'ready', 'playing'} or state.get('playMode') not in {'stopped', 'playing', 'paused'}:
            raise ProbeRefusal('EDITOR_NOT_READY', 'Editor must report ready or playing')
        if expected is not None and state['playMode'] != expected:
            raise ProbeRefusal('PROBE_PLAY_CHANGED', 'Editor play state changed during the probe')
        return state

    def _wait_play(self, lease, report, expected):
        while True:
            state = self._native(lease, report, 'editor', 'status').value
            if isinstance(state, dict) and state.get('playMode') == expected:
                self._state(lease, report, expected=expected)
                return
            self.session.clock.sleep(min(0.1, lease.remaining()))

    def _run_script(self, lease, report, arguments, dry_run):
        """`script run`; a compile failure is read from a dry run of the same file, because the failed reply carries no diagnostics."""
        result = self._native(lease, report, 'script', 'run', arguments, allow_failure=True)
        if result.ok:
            self._script(result.value, dry_run)
            return
        if not dry_run and 'entry point threw' in (result.message or ''):
            raise ProbeRefusal('PROBE_RUNTIME_FAILED', 'The probe entry point threw', result.evidence())
        if not dry_run and result.exit_code in {11, 13}:
            raise ProbeRefusal('PROBE_ENTRY_FAILED', result.message or 'The probe file or entry point was not found', result.evidence())
        if result.status != 'FAILED':
            failure = native_failure(result, 'script run')
            raise ProbeRefusal(failure.code, str(failure), failure.details)
        check = self._native(lease, report, 'script', 'run', {**arguments, 'dry_run': True}, human=True, allow_failure=True)
        if check.exit_code == 6:
            raise ProbeRefusal('PROBE_COMPILE_FAILED', 'The probe did not compile', {'compilerOutput': check.stdout, 'compilerErrors': check.stderr})
        raise ProbeRefusal('PROBE_SCRIPT_FAILED', result.message or 'script run failed', result.evidence())

    @staticmethod
    def _script(value, dry_run):
        if not isinstance(value, dict) or 'result' not in value:
            raise ProbeRefusal('PROBE_SCRIPT_RESPONSE_INVALID', 'script run omitted its result')
        if not isinstance(value.get('diagnostics'), list) or any(not ConsoleWorkflow._nonnegative_integer(value.get(key)) for key in ('compileMs', 'executeMs')):
            raise ProbeRefusal('PROBE_SCRIPT_RESPONSE_INVALID', 'script run omitted structured diagnostics or timing evidence')
        for diagnostic in value['diagnostics']:
            if not isinstance(diagnostic, dict) or diagnostic.get('severity') not in {'error', 'warning', 'info'}:
                raise ProbeRefusal('PROBE_SCRIPT_RESPONSE_INVALID', 'script run returned an invalid compiler diagnostic')
            if diagnostic['severity'] == 'error':
                raise ProbeRefusal('PROBE_COMPILE_FAILED', 'script run returned a compiler error diagnostic', {'diagnostic': diagnostic})
        if dry_run:
            if value.get('assemblyName') is not None or value['executeMs'] != 0:
                raise ProbeRefusal('PROBE_SCRIPT_RESPONSE_INVALID', 'Compile-only response must show no loaded assembly or execution')
        elif not isinstance(value.get('assemblyName'), str) or not value['assemblyName'].strip():
            raise ProbeRefusal('PROBE_SCRIPT_RESPONSE_INVALID', 'script run must identify its compiled assembly')

    @staticmethod
    def _verify_sources(owner, sources, report, lease):
        if owner._hash_sources(sources, check_budget=lease.remaining) != report['sources']:
            raise ProbeRefusal('PROBE_SOURCE_CHANGED', 'Source hash changed during the probe')
