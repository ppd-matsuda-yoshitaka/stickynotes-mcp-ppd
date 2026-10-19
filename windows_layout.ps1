$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase
    $refs = @([System.Windows.Automation.AutomationElement].Assembly.Location,
              [System.Windows.Automation.ControlType].Assembly.Location,
              [System.Windows.Rect].Assembly.Location, 'System.Core')
    Add-Type -Path (Join-Path $PSScriptRoot 'windows_layout.cs') -ReferencedAssemblies $refs
    [StickyLayoutNative]::Initialize()
    $monitors = @([StickyLayoutNative]::Monitors())
    $windows = @([StickyLayoutNative]::Scan())
    if ($request.action -eq 'scan') {
        @{monitors=$monitors; windows=$windows} | ConvertTo-Json -Depth 12 -Compress
    } elseif ($request.action -eq 'move') {
        function MonitorKey($items) {
            return (($items | Sort-Object device | ForEach-Object { "$($_.device):$($_.dpi):$($_.work -join ',')" }) -join ';')
        }
        if ((MonitorKey $request.monitors) -cne (MonitorKey $monitors)) { throw 'Monitor topology/work area/DPI changed; no windows moved' }
        # Validate every identity/body/rectangle before the first move, including handle reuse.
        $handles = @{}
        foreach ($move in $request.moves) {
            $e = $move.expected
            $found = @($windows | Where-Object { $_.hwnd -eq $e.hwnd })
            if ($handles.ContainsKey([string]$e.hwnd)) { throw 'Duplicate window target' }
            $handles[[string]$e.hwnd] = $true
            if ($found.Count -ne 1 -or $found[0].identity -cne $e.identity -or $found[0].body -cne $e.body -or
                ($found[0].rect -join ',') -cne ($e.rect -join ',') -or
                ($found[0].frame -join ',') -cne ($e.frame -join ',')) { throw 'Note identity/body/geometry changed; no windows moved' }
        }
        $attempted = [System.Collections.Generic.List[object]]::new()
        try {
            foreach ($move in $request.moves) {
                # Recheck identity immediately before each action as well.
                $current = @([StickyLayoutNative]::Scan() | Where-Object { $_.hwnd -eq $move.expected.hwnd })
                if ($current.Count -ne 1 -or $current[0].identity -cne $move.expected.identity -or
                    $current[0].body -cne $move.expected.body -or
                    ($current[0].rect -join ',') -cne ($move.expected.rect -join ',') -or
                    ($current[0].frame -join ',') -cne ($move.expected.frame -join ',')) { throw 'Note changed during layout' }
                $attempted.Add($move)
                [StickyLayoutNative]::Move($move.expected.hwnd, [int[]]$move.rect)
            }
            if ((MonitorKey $request.monitors) -cne (MonitorKey @([StickyLayoutNative]::Monitors()))) { throw 'Monitor topology changed during layout' }
            $after = @([StickyLayoutNative]::Scan())
            foreach ($move in $request.moves) {
                $actual = @($after | Where-Object { $_.hwnd -eq $move.expected.hwnd })
                if ($actual.Count -ne 1 -or $actual[0].identity -cne $move.expected.identity -or
                    $actual[0].body -cne $move.expected.body -or
                    ($actual[0].rect -join ',') -cne ($move.rect -join ',')) { throw 'Final verification failed' }
            }
            @{verified=$true; count=$request.moves.Count} | ConvertTo-Json -Compress
        } catch {
            $reason = $_.Exception.Message
            $rollbackFailures = [System.Collections.Generic.List[string]]::new()
            for ($i=$attempted.Count-1; $i -ge 0; $i--) {
                $move=$attempted[$i]
                try {
                    $current = @([StickyLayoutNative]::Scan() | Where-Object { $_.hwnd -eq $move.expected.hwnd })
                    if ($current.Count -ne 1 -or $current[0].identity -cne $move.expected.identity -or
                        $current[0].body -cne $move.expected.body) { throw 'Identity changed; rollback refused' }
                    [StickyLayoutNative]::Move($move.expected.hwnd, [int[]]$move.expected.rect)
                } catch { $rollbackFailures.Add("$($move.expected.hwnd): $($_.Exception.Message)") }
            }
            throw "$reason; rollback failures: $($rollbackFailures -join '; ')"
        }
    } else { throw 'Unknown helper action' }
} catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 1
}
