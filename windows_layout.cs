// Used only by the short-lived PowerShell helper. Windows/.NET built-ins only.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Automation;

public static class StickyLayoutNative {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X,Y; }
    [StructLayout(LayoutKind.Sequential)] public struct MINMAXINFO {
        public POINT reserved, maxSize, maxPosition, minTrack, maxTrack;
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct MONITORINFO {
        public int size; public RECT monitor, work; public uint flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string device;
    }
    public class Monitor { public string device; public int[] work; public uint dpi; }
    public class Window {
        public long hwnd; public string identity, body, device; public int[] rect, frame, minimum;
    }
    delegate bool EnumWindow(IntPtr h, IntPtr p);
    delegate bool EnumMonitor(IntPtr h, IntPtr dc, ref RECT r, IntPtr p);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindow callback, IntPtr p);
    [DllImport("user32.dll")] static extern bool EnumDisplayMonitors(IntPtr dc, IntPtr clip, EnumMonitor callback, IntPtr p);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern bool GetMonitorInfo(IntPtr h, ref MONITORINFO info);
    [DllImport("shcore.dll")] static extern int GetDpiForMonitor(IntPtr h, int type, out uint x, out uint y);
    [DllImport("shcore.dll")] static extern int SetProcessDpiAwareness(int awareness);
    [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern IntPtr MonitorFromWindow(IntPtr h, uint flags);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsZoomed(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", SetLastError=true)] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr h, uint attribute, out RECT r, int size);
    [DllImport("user32.dll", SetLastError=true)] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int height, uint flags);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, ref MINMAXINFO info, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll")] static extern int GetSystemMetricsForDpi(int index, uint dpi);

    public static void Initialize() {
        // New helper process: no server thread's DPI context is modified.
        int hr = SetProcessDpiAwareness(2);
        if (hr != 0) throw new InvalidOperationException("Cannot establish per-monitor process DPI awareness: " + hr);
        if (SetThreadDpiAwarenessContext(new IntPtr(-4)) == IntPtr.Zero)
            throw new InvalidOperationException("Cannot establish per-monitor V2 thread DPI awareness");
    }
    static MONITORINFO Info(IntPtr h) {
        var i = new MONITORINFO(); i.size = Marshal.SizeOf(i);
        if (!GetMonitorInfo(h, ref i)) throw new InvalidOperationException("Monitor query failed");
        return i;
    }
    public static Monitor[] Monitors() {
        var monitors = new List<Monitor>();
        if (!EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, delegate(IntPtr h, IntPtr dc, ref RECT r, IntPtr p) {
            var i = Info(h); uint x,y;
            if (GetDpiForMonitor(h, 0, out x, out y) != 0 || x == 0 || x != y)
                throw new InvalidOperationException("Cannot determine monitor DPI");
            monitors.Add(new Monitor { device=i.device, work=new int[]{i.work.L,i.work.T,i.work.R,i.work.B}, dpi=x });
            return true;
        }, IntPtr.Zero)) throw new InvalidOperationException("Cannot enumerate monitors");
        if (monitors.Count == 0) throw new InvalidOperationException("No interactive monitor available");
        return monitors.ToArray();
    }
    public static int[] Rectangle(long handle) {
        RECT r;
        if (!GetWindowRect(new IntPtr(handle), out r)) throw new InvalidOperationException("Window rectangle query failed");
        return new int[]{r.L,r.T,r.R-r.L,r.B-r.T};
    }
    static int[] Frame(IntPtr handle) {
        RECT r;
        if (DwmGetWindowAttribute(handle,9,out r,Marshal.SizeOf(typeof(RECT))) != 0)
            throw new InvalidOperationException("Cannot determine visible window frame");
        return new int[]{r.L,r.T,r.R-r.L,r.B-r.T};
    }
    static string ProcessIdentity(int pid) {
        using (var p = Process.GetProcessById(pid)) { return p.Id + ":" + p.StartTime.ToUniversalTime().Ticks; }
    }
    static bool IsNotes(int pid) {
        using (var p = Process.GetProcessById(pid)) { return String.Equals(p.ProcessName,"Microsoft.Notes",StringComparison.OrdinalIgnoreCase); }
    }
    public static Window[] Scan() {
        var windows = new List<Window>();
        Exception failure = null;
        bool ok = EnumWindows(delegate(IntPtr h, IntPtr unused) {
            try {
                if (!IsWindowVisible(h)) return true;
                uint owner; GetWindowThreadProcessId(h, out owner);
                string ownerName;
                try { using (var p = Process.GetProcessById((int)owner)) ownerName = p.ProcessName; }
                catch (ArgumentException) { return true; }
                if (!String.Equals(ownerName,"Microsoft.Notes",StringComparison.OrdinalIgnoreCase) &&
                    !String.Equals(ownerName,"ApplicationFrameHost",StringComparison.OrdinalIgnoreCase)) return true;
                var root = AutomationElement.FromHandle(h);
                var condition = new OrCondition(
                    new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.Document),
                    new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.Edit));
                var controls = root.FindAll(TreeScope.Descendants,condition);
                var bodies = new List<string>(); var identities = new List<string>();
                foreach (AutomationElement e in controls) {
                    int pid = e.Current.ProcessId;
                    if (!IsNotes(pid)) continue;
                    object pattern;
                    // A document/edit with TextPattern, never a title or preview label.
                    if (!e.TryGetCurrentPattern(TextPattern.Pattern,out pattern)) continue;
                    bodies.Add(((TextPattern)pattern).DocumentRange.GetText(-1));
                    identities.Add(ProcessIdentity(pid) + ":" + String.Join(".",e.GetRuntimeId()));
                }
                if (bodies.Count == 0) return true; // Includes the notes-list window.
                if (bodies.Count != 1) throw new InvalidOperationException("Ambiguous note editor in window " + h);
                if (IsIconic(h) || IsZoomed(h)) throw new InvalidOperationException("Restore minimized/maximized notes manually first");
                var monitor = Info(MonitorFromWindow(h,2)); uint dx,dy;
                if (GetDpiForMonitor(MonitorFromWindow(h,2),0,out dx,out dy) != 0)
                    throw new InvalidOperationException("Cannot query note monitor DPI");
                var minimum = new MINMAXINFO();
                minimum.minTrack.X = GetSystemMetricsForDpi(34,dx);
                minimum.minTrack.Y = GetSystemMetricsForDpi(35,dx);
                IntPtr ignored;
                if (SendMessageTimeout(h,0x24,IntPtr.Zero,ref minimum,2,1000,out ignored) == IntPtr.Zero)
                    throw new InvalidOperationException("App minimum-size query timed out or was denied");
                windows.Add(new Window { hwnd=h.ToInt64(), body=bodies[0],
                    identity=ProcessIdentity((int)owner) + ":" + String.Join(".",root.GetRuntimeId()) + ":" + identities[0],
                    rect=Rectangle(h.ToInt64()), frame=Frame(h), device=monitor.device,
                    minimum=new int[]{minimum.minTrack.X,minimum.minTrack.Y} });
                return true;
            } catch (Exception ex) { failure=ex; return false; }
        },IntPtr.Zero);
        if (failure != null) throw new InvalidOperationException("Cannot safely inspect notes",failure);
        if (!ok) throw new InvalidOperationException("Window enumeration failed");
        return windows.ToArray();
    }
    public static void Move(long handle, int[] rect) {
        // Async prevents a hung app from blocking this helper indefinitely. No focus/Z-order changes.
        if (!SetWindowPos(new IntPtr(handle),IntPtr.Zero,rect[0],rect[1],rect[2],rect[3],0x4014))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        for (int i=0;i<40;i++) {
            Thread.Sleep(50);
            var actual=Rectangle(handle);
            if (String.Join(",",actual)==String.Join(",",rect)) return;
        }
        throw new InvalidOperationException("App did not accept the requested geometry (minimum size/DPI/access); verification failed");
    }
}
