# Starts a program on a desktop of its own that is never shown, and prints its process id. Its windows draw as usual (play,
# the Game view, screen captures), but whatever they do, they cannot take the screen: a window can only come to the front on
# its own desktop, and this one is never switched to. Worktree Editors run here (unity_worktree.py), because a windowed
# Editor brings its main window to the front each time play starts, whatever started it and however its Game view is set.
# With -ListPid instead, it prints every visible top-level window that process has on the desktop as one JSON array of
# {desktop, class, title, modal, buttons}, modal being a #32770 window whose owner is disabled: a dialog an Editor raises on the hidden desktop is seen by
# nobody, and its main thread waits on it. A splash or progress window is not one. -Desktop Default lists the interactive desktop instead.
# With -PressPid, -Dialog and -Button it presses that button on that process's one dialog with that exact title.
param([string]$CommandLine, [int]$ListPid = 0, [int]$PressPid = 0, [string]$Dialog = '', [string]$Button = '', [string]$Desktop = 'UnityCliWorktrees')
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class HiddenDesktop {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct StartupInfo { public int cb; public string reserved, desktop, title; public int x, y, width, height, columns, rows, fill, flags; public short show, reserved2; public IntPtr reserved3, input, output, error; }
  [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process, thread; public int id, threadId; }
  [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr OpenDesktop(string name, int flags, bool inherit, uint access);
  [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateDesktop(string name, IntPtr device, IntPtr mode, int flags, uint access, IntPtr security);
  [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
  [DllImport("user32.dll")] static extern uint WaitForInputIdle(IntPtr process, uint milliseconds);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool CreateProcess(string application, string commandLine, IntPtr processAttributes, IntPtr threadAttributes, bool inherit, uint flags, IntPtr environment, string directory, ref StartupInfo startup, out ProcessInfo info);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  delegate bool EnumProc(IntPtr window, IntPtr data);
  [DllImport("user32.dll")] static extern bool EnumDesktopWindows(IntPtr desktop, EnumProc callback, IntPtr data);
  [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr window, EnumProc callback, IntPtr data);
  [DllImport("user32.dll")] static extern int GetWindowThreadProcessId(IntPtr window, out int process);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr window, System.Text.StringBuilder text, int size);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr window, System.Text.StringBuilder text, int size);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr window);
  [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr window);
  [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr window, uint relation);
  [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
  const uint AllAccess = 0x10000000;

  static string Text(IntPtr window) { var t = new System.Text.StringBuilder(512); GetWindowText(window, t, 512); return t.ToString(); }
  static string Class(IntPtr window) { var t = new System.Text.StringBuilder(128); GetClassName(window, t, 128); return t.ToString(); }

  static string Quote(string s) {
    var b = new System.Text.StringBuilder("\"");
    foreach (var c in s) {
      if (c == '"' || c == '\\') b.Append('\\').Append(c);
      else if (c < 0x20) b.Append("\\u").Append(((int)c).ToString("x4"));
      else b.Append(c);
    }
    return b.Append('"').ToString();
  }

  /// A modal dialog: a standard dialog window (class #32770) whose owner window is disabled while it is open. Measured on
  /// an Editor: "Enter Safe Mode?" and EditorUtility.DisplayDialog are #32770 with a disabled owner. The startup splash
  /// ("Opening project...", class UnitySplashWindow, with a button) and the progress windows of an import or a domain
  /// reload ("Hold on...", "Reloading Domain": #32770 but with no owner) are busy states, never dialogs to answer.
  static bool IsModalDialog(IntPtr w) {
    if (!IsWindowVisible(w) || Class(w) != "#32770") return false;
    var owner = GetWindow(w, 4);
    return owner != IntPtr.Zero && !IsWindowEnabled(owner);
  }

  /// Every visible top-level window of this process on the desktop, as a JSON array of {desktop, class, title, modal,
  /// buttons}; buttons are the visible, enabled ones, and only a window with modal true is a dialog to answer.
  public static string Windows(string desktop, int process) {
    var items = new System.Collections.Generic.List<string>();
    var handle = OpenDesktop(desktop, 0, false, AllAccess);
    if (handle == IntPtr.Zero) return "[]";
    try {
      EnumDesktopWindows(handle, (w, _) => {
        int owner; GetWindowThreadProcessId(w, out owner);
        if (owner != process || !IsWindowVisible(w)) return true;
        var buttons = new System.Collections.Generic.List<string>();
        EnumChildWindows(w, (c, __) => { if (Class(c) == "Button" && IsWindowVisible(c) && IsWindowEnabled(c)) buttons.Add(Quote(Text(c))); return true; }, IntPtr.Zero);
        items.Add("{\"desktop\":" + Quote(desktop) + ",\"class\":" + Quote(Class(w)) + ",\"title\":" + Quote(Text(w)) + ",\"modal\":" + (IsModalDialog(w) ? "true" : "false") + ",\"buttons\":[" + string.Join(",", buttons.ToArray()) + "]}");
        return true;
      }, IntPtr.Zero);
    } finally { CloseDesktop(handle); }
    return "[" + string.Join(",", items.ToArray()) + "]";
  }

  /// One modal dialog of this process titled exactly dialog, and one button on it labelled exactly button: pressed. Anything else is refused.
  public static string Press(string desktop, int process, string dialog, string button) {
    var handle = OpenDesktop(desktop, 0, false, AllAccess);
    if (handle == IntPtr.Zero) return "desktop not found";
    var dialogs = new System.Collections.Generic.List<IntPtr>();
    var buttons = new System.Collections.Generic.List<IntPtr>();
    try {
      EnumDesktopWindows(handle, (w, _) => {
        int owner; GetWindowThreadProcessId(w, out owner);
        if (owner != process || !IsModalDialog(w)) return true;
        if (Text(w) == dialog) dialogs.Add(w);
        return true;
      }, IntPtr.Zero);
      if (dialogs.Count != 1) return "refused: " + dialogs.Count + " " + dialog + " dialogs";
      EnumChildWindows(dialogs[0], (c, __) => { if (Class(c) == "Button" && Text(c) == button) buttons.Add(c); return true; }, IntPtr.Zero);
      if (buttons.Count != 1) return "refused: " + buttons.Count + " " + button + " buttons";
      SendMessage(buttons[0], 0x00F5, IntPtr.Zero, IntPtr.Zero);
      return "pressed";
    } finally { CloseDesktop(handle); }
  }

  public static int Start(string desktop, string commandLine) {
    var handle = OpenDesktop(desktop, 0, false, AllAccess);
    if (handle == IntPtr.Zero) handle = CreateDesktop(desktop, IntPtr.Zero, IntPtr.Zero, 0, AllAccess, IntPtr.Zero);
    if (handle == IntPtr.Zero) throw new Exception("desktop " + desktop + " not made: error " + Marshal.GetLastWin32Error());
    try {
      var startup = new StartupInfo { desktop = "WinSta0\\" + desktop };
      startup.cb = Marshal.SizeOf(startup);
      ProcessInfo info;
      if (!CreateProcess(null, commandLine, IntPtr.Zero, IntPtr.Zero, false, 0, IntPtr.Zero, null, ref startup, out info))
        throw new Exception("not started: error " + Marshal.GetLastWin32Error());
      // A desktop goes away with its last handle unless a program is on it: this one is held until the program is up
      // and waiting for input (an Editor started on a desktop already gone exits before writing its log).
      WaitForInputIdle(info.process, 120000);
      CloseHandle(info.thread);
      CloseHandle(info.process);
      return info.id;
    } finally {
      CloseDesktop(handle);
    }
  }
}
"@
if ($PressPid) { [HiddenDesktop]::Press($Desktop, $PressPid, $Dialog, $Button) } elseif ($ListPid) { [HiddenDesktop]::Windows($Desktop, $ListPid) } else { [HiddenDesktop]::Start($Desktop, $CommandLine) }
