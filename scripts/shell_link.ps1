# Windows shortcuts (.lnk) with full Unicode paths: Set-DeltaShortcut writes one, Get-DeltaShortcut reads one back.
#
# Why not WScript.Shell alone: its shortcut object stores the target through the ANSI code page, so a letter the
# code page lacks is replaced by its nearest plain letter. A Delta installed under C:\Users\Raj with a macron on
# the a (or a Telugu, Hindi or Chinese user name) got a Desktop icon pointing at a folder that does not exist, and
# clicking it did nothing. Found by tests/e2e/test_installer_policy_e2e.py on the CI runner. These functions use the
# shell's own IShellLinkW (the Unicode interface Explorer uses); if C# cannot be compiled here (a locked-down
# PowerShell), they fall back to WScript.Shell, which is right for every ASCII path.
#
# Only defines functions; create_shortcut.ps1 and the tests run this file's text (execution policy: Restricted
# refuses script files, not text). Keep this file ASCII (Windows PowerShell 5.1 reads it in the ANSI code page).

function Initialize-DeltaShellLink {
    if ("DeltaSetup.ShellLink" -as [type]) { return $true }
    try {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace DeltaSetup {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch, IntPtr pfd, int fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out short pwHotkey);
        void SetHotkey(short wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch, out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, int dwReserved);
        void Resolve(IntPtr hwnd, int fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class CShellLink { }

    public static class ShellLink {
        public static void Save(string path, string target, string arguments, string workingDir, string description,
                                string iconPath) {
            IShellLinkW link = (IShellLinkW)new CShellLink();
            try {
                link.SetPath(target);
                link.SetArguments(arguments ?? "");
                link.SetWorkingDirectory(workingDir ?? "");
                link.SetDescription(description ?? "");
                if (!String.IsNullOrEmpty(iconPath)) { link.SetIconLocation(iconPath, 0); }
                ((IPersistFile)link).Save(path, true);
            } finally { Marshal.ReleaseComObject(link); }
        }

        public static string[] Read(string path) {
            IShellLinkW link = (IShellLinkW)new CShellLink();
            try {
                ((IPersistFile)link).Load(path, 0);
                StringBuilder target = new StringBuilder(32768), args = new StringBuilder(32768),
                              dir = new StringBuilder(32768);
                link.GetPath(target, target.Capacity, IntPtr.Zero, 4);  // SLGP_RAWPATH: as stored, not resolved
                link.GetArguments(args, args.Capacity);
                link.GetWorkingDirectory(dir, dir.Capacity);
                return new string[] { target.ToString(), args.ToString(), dir.ToString() };
            } finally { Marshal.ReleaseComObject(link); }
        }
    }
}
"@ -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Set-DeltaShortcut([string]$Path, [string]$Target, [string]$Arguments = "", [string]$WorkingDirectory = "",
                           [string]$Description = "", [string]$Icon = "") {
    # Returns how it was written: "unicode" (IShellLinkW) or "ansi" (WScript.Shell fallback).
    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Path))
    if (Initialize-DeltaShellLink) {
        [DeltaSetup.ShellLink]::Save($Path, $Target, $Arguments, $WorkingDirectory, $Description, $Icon)
        return "unicode"
    }
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $WorkingDirectory
    $lnk.Description = $Description
    if ($Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
    if ($Target -cne [System.Text.Encoding]::Default.GetString([System.Text.Encoding]::Default.GetBytes($Target))) {
        Write-Warning ("The shortcut $Path could not store every letter of $Target (this PowerShell cannot use the " +
                       "Unicode shortcut interface), so it may not open. Start Delta with the delta command instead.")
    }
    return "ansi"
}

function Get-DeltaShortcut([string]$Path) {
    # @{ Target; Arguments; WorkingDirectory } exactly as stored (Unicode), or $null when it cannot be read.
    if (-not (Initialize-DeltaShellLink)) { return $null }
    $r = [DeltaSetup.ShellLink]::Read($Path)
    return [pscustomobject]@{ Target = $r[0]; Arguments = $r[1]; WorkingDirectory = $r[2] }
}
