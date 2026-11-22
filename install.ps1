# Fire Emblem: Fortune's Weave (EmblemWeave / fefw-recomp) installer for Windows (driven by NUP Studio)
param(
    [string]$Nup = "",
    [string]$Dir = "",
    [string]$ShortcutName = "Fire Emblem Fortune's Weave",
    [string]$IconPath = "",
    [string]$LauncherUiPath = "",
    [switch]$NoDesktopShortcut
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}
function Say($msg) { Write-Host "[FEFW-Recomp] $msg" -ForegroundColor Cyan }

if (-not $Nup -or -not (Test-Path -LiteralPath $Nup)) {
    Say "error: missing .nup file or game folder: $Nup"
    exit 1
}
if (-not $Dir) {
    $Dir = Join-Path $env:LOCALAPPDATA "Switch2Recomp\Fire Emblem Fortune's Weave"
}
$Dir = [System.IO.Path]::GetFullPath($Dir)
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Say "installing into $Dir"

$source = $PSScriptRoot
$globalMsysInstaller = Join-Path $source "..\..\install_msys2.ps1"
$clang64Prefix = "C:\msys64\clang64"
$clang64Bin = Join-Path $clang64Prefix "bin"

if (-not (Test-Path -LiteralPath (Join-Path $clang64Bin "clang++.exe")) -or
    -not (Test-Path -LiteralPath (Join-Path $clang64Bin "llvm-ar.exe")) -or
    -not (Test-Path -LiteralPath (Join-Path $clang64Bin "llvm-rc.exe")) -or
    -not (Test-Path -LiteralPath (Join-Path $clang64Bin "python.exe")) -or
    -not (Test-Path -LiteralPath (Join-Path $clang64Prefix "include\zstd.h"))) {
    if (-not (Test-Path -LiteralPath $globalMsysInstaller)) {
        Say "error: MSYS2 missing and global installer not found: $globalMsysInstaller"
        exit 1
    }
    & $globalMsysInstaller -Msys "C:\msys64"
    if ($LASTEXITCODE -ne 0) {
        Say "MSYS2 installation cancelled or failed; stopping recompilation."
        exit 1
    }
}

$pyExe = Join-Path $clang64Bin "python.exe"
$fefwBuildPy = Join-Path $source "tools\build\fefw_build.py"
$extractNupPy = Join-Path $source "tools\build\extract_nup_fefw.py"
$extractCorpusPy = Join-Path $source "tools\shaders\extract_corpus.py"
$spirvHarnessExe = Join-Path $source "tools\shaders\spirv_harness.exe"
$rtDllSrc = Join-Path $source "runtime\fefw_rt.dll"

foreach ($req in @($pyExe, $fefwBuildPy, $extractNupPy, $extractCorpusPy, $spirvHarnessExe, $rtDllSrc)) {
    if (-not (Test-Path -LiteralPath $req)) {
        Say "error: required component missing: $req"
        exit 1
    }
}

$env:PATH = "$clang64Bin;" + $env:PATH
$env:FEFW_CLANG_BIN = $clang64Bin
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHON_JIT = "0"
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue

# Ensure capstone & lz4 are present in MSYS2 Clang64 Python (standard MSYS2 packages)
& $pyExe -c "import capstone, lz4" 2>$null
if ($LASTEXITCODE -ne 0) {
    $pacmanExe = "C:\msys64\usr\bin\pacman.exe"
    if (Test-Path -LiteralPath $pacmanExe) {
        Say "installing MSYS2 Python packages (capstone, lz4)..."
        & $pacmanExe -S --noconfirm --needed mingw-w64-clang-x86_64-python-capstone mingw-w64-clang-x86_64-python-lz4
    }
    & $pyExe -c "import capstone, lz4" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say "error: MSYS2 Python is missing capstone or lz4."
        exit 1
    }
}

# Pin to E-cores on Intel 13th/14th-gen for heavy Python/C++ codegen stability
$proc = [System.Diagnostics.Process]::GetCurrentProcess()
$jobs = [Math]::Max(1, [Math]::Min([Environment]::ProcessorCount - 1, 12))
try {
    $cpuName = (Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)
    if ($cpuName -match 'Intel.*1[34]\d{3}' -and [Environment]::ProcessorCount -gt 16) {
        $eCoreMask = [int64]0
        for ($c = 16; $c -lt [Math]::Min(63, [Environment]::ProcessorCount); $c++) {
            $eCoreMask = $eCoreMask -bor ([int64]1 -shl $c)
        }
        if ($eCoreMask -gt 0) {
            Say "detected ${cpuName}: pinning codegen & build to E-cores for stability"
            $proc.ProcessorAffinity = [IntPtr]$eCoreMask
            $jobs = [Math]::Max(1, [Math]::Min([Environment]::ProcessorCount - 16, 12))
        }
    }
} catch {}

# ---- Global Saves folder (<ParentDir>\Saves\<GameFolderName>) ----
$parentInstallDir = Split-Path -Parent $Dir
$globalSavesRoot = Join-Path $parentInstallDir "Saves"
$globalSaveDir = Join-Path $globalSavesRoot (Split-Path -Leaf $Dir)
New-Item -ItemType Directory -Force -Path $globalSaveDir | Out-Null

# ---- 1. Prepare ExeFS and shader sources (supports both .nup and extracted source folder) ----
$workRoot = Join-Path $Dir "_work"
New-Item -ItemType Directory -Force -Path $workRoot | Out-Null

$resolvedRomPath = [System.IO.Path]::GetFullPath($Nup)
$srcExefsDir = ""
$romfsForGame = ""
$romfsForShaders = ""
$shaderCacheDir = Join-Path $Dir "shader_cache"
$needShaders = -not (Test-Path -LiteralPath (Join-Path $shaderCacheDir "results.tsv"))

if (Test-Path -LiteralPath $Nup -PathType Container) {
    $dumpRoot = $resolvedRomPath
    $updatesDir = Join-Path $dumpRoot "updates"
    if (Test-Path -LiteralPath $updatesDir -PathType Container) {
        $bestUpd = Get-ChildItem -LiteralPath $updatesDir -Directory |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "exefs\main") } |
            Sort-Object { if ($_.Name -match '^\d+$') { [int64]$_.Name } else { -1 } } -Descending |
            Select-Object -First 1
        if ($bestUpd) {
            Say "detected update folder: $($bestUpd.FullName)"
            $dumpRoot = $bestUpd.FullName
            $resolvedRomPath = $bestUpd.FullName
        }
    }
    $srcExefsDir = if (Test-Path -LiteralPath (Join-Path $dumpRoot "exefs\main")) { Join-Path $dumpRoot "exefs" } else { $dumpRoot }
    $romfsForGame = Join-Path $dumpRoot "romfs"
    $romfsForShaders = $romfsForGame
    if (-not (Test-Path -LiteralPath (Join-Path $srcExefsDir "main")) -or -not (Test-Path -LiteralPath $romfsForGame)) {
        Say "error: source folder must contain exefs/main and romfs/: $dumpRoot"
        exit 1
    }
    Say "using source folder: $dumpRoot"
} else {
    $extractTmp = Join-Path $workRoot "nup_extract"
    New-Item -ItemType Directory -Force -Path $extractTmp | Out-Null
    $extractArgs = @($extractNupPy, $resolvedRomPath, "--out", $extractTmp)
    if ($needShaders) {
        $extractArgs += "--shaders"
        Say "extracting ExeFS and shader archives from .nup (RomFS stays inside .nup)..."
    } else {
        Say "extracting ExeFS from .nup (RomFS stays inside .nup)..."
    }
    & $pyExe @extractArgs
    if ($LASTEXITCODE -ne 0) {
        Say "error: extract_nup_fefw.py failed ($LASTEXITCODE)"
        exit 1
    }
    $srcExefsDir = Join-Path $extractTmp "exefs"
    $romfsForGame = $resolvedRomPath
    $romfsForShaders = Join-Path $extractTmp "shader_romfs"
}

# ---- 2. Lift ARM64 ExeFS and build fefw.exe ----
$fefwExe = Join-Path $Dir "fefw.exe"
Say "recompiling Fire Emblem: Fortune's Weave (lift + compile + link)..."
& $pyExe $fefwBuildPy --exefs $srcExefsDir --game $Dir --jobs $jobs
if ($LASTEXITCODE -ne 0) {
    Say "error: fefw_build.py failed ($LASTEXITCODE)"
    exit 1
}
if (-not (Test-Path -LiteralPath $fefwExe)) {
    Say "error: fefw.exe was not produced"
    exit 1
}

# ---- 3. Copy runtime DLL, fonts, and locales ----
Say "installing runtime files (fefw_rt.dll, fonts, locales)..."
Copy-Item -LiteralPath $rtDllSrc -Destination (Join-Path $Dir "fefw_rt.dll") -Force
$renhdrumSrc = Join-Path $source "runtime\renhdrum.dll"
if (Test-Path -LiteralPath $renhdrumSrc) {
    Copy-Item -LiteralPath $renhdrumSrc -Destination (Join-Path $Dir "renhdrum.dll") -Force
}
$fontsDst = Join-Path $Dir "fonts"
New-Item -ItemType Directory -Force -Path $fontsDst | Out-Null
Copy-Item -Path (Join-Path $source "runtime\fonts\*") -Destination $fontsDst -Force -ErrorAction SilentlyContinue
$localesDst = Join-Path $Dir "locales"
New-Item -ItemType Directory -Force -Path $localesDst | Out-Null
Copy-Item -Path (Join-Path $source "runtime\locales\*") -Destination $localesDst -Force -ErrorAction SilentlyContinue

# ---- 4. Extract & translate Ampere shaders into shader_cache/ ----
if ($needShaders) {
    New-Item -ItemType Directory -Force -Path $shaderCacheDir | Out-Null
    $corpusDir = Join-Path $workRoot "shader_corpus"
    $programsBin = Join-Path $shaderCacheDir "programs.bin"
    Say "extracting Ampere shader corpus..."
    & $pyExe $extractCorpusPy `
        --romfs $romfsForShaders `
        --main (Join-Path $Dir "exefs\main") `
        --exefs (Join-Path $Dir "exefs") `
        --out $corpusDir `
        --jobs $jobs `
        --programs-bin $programsBin
    if ($LASTEXITCODE -ne 0) {
        Say "error: extract_corpus.py failed ($LASTEXITCODE)"
        exit 1
    }
    Say "translating shaders to SPIR-V (spirv_harness)..."
    & $spirvHarnessExe --batch $corpusDir --out $shaderCacheDir
    if ($LASTEXITCODE -ne 0) {
        Say "error: spirv_harness.exe failed ($LASTEXITCODE)"
        exit 1
    }
} else {
    Say "shader_cache already present, skipping shader translation."
}

# ---- 5. Write fefw.ini and default controls.ini (720p windowed) ----
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$fefwIniPath = Join-Path $Dir "fefw.ini"
$fefwIniText = @"
[fs]
exefs = exefs
romfs = $romfsForGame
save_dir = $globalSaveDir

[nvn]
shader_cache = shader_cache
"@
[System.IO.File]::WriteAllText($fefwIniPath, $fefwIniText, $utf8NoBom)

$controlsIniPath = Join-Path $Dir "controls.ini"
if (-not (Test-Path -LiteralPath $controlsIniPath)) {
    $controlsIniText = @"
[display]
window_mode = windowed
width = 1280
height = 720
fps_limit = 60
vsync = false
"@
    [System.IO.File]::WriteAllText($controlsIniPath, $controlsIniText, $utf8NoBom)
}

# ---- 6. Pre-launch UI, config, icons, shortcuts ----
$repoRoot = Resolve-Path (Join-Path $source "..\..\..")
$launcherUiSrc = $LauncherUiPath
if (-not $launcherUiSrc -or -not (Test-Path -LiteralPath $launcherUiSrc)) {
    $toolsExe = Join-Path $repoRoot "tools\game_launcher.exe"
    $exeCand = Join-Path $repoRoot "game_launcher.exe"
    if (Test-Path -LiteralPath $toolsExe) { $launcherUiSrc = $toolsExe }
    elseif (Test-Path -LiteralPath $exeCand) { $launcherUiSrc = $exeCand }
    else { $launcherUiSrc = Join-Path $repoRoot "src\game_launcher.py" }
}
$isLauncherExe = $launcherUiSrc -match "\.exe$"
$launcherUiDst = Join-Path $Dir $(if ($isLauncherExe) { "game_launcher.exe" } else { "game_launcher.py" })
if (Test-Path -LiteralPath $launcherUiSrc) {
    Copy-Item -LiteralPath $launcherUiSrc -Destination $launcherUiDst -Force
}

$launchConfigPath = Join-Path $Dir "launch_options.json"
$tplPath = Join-Path $source "launch_options_template.json"
if (Test-Path -LiteralPath $tplPath) {
    $tpl = Get-Content -LiteralPath $tplPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $tpl | Add-Member -NotePropertyName "working_dir" -NotePropertyValue $Dir -Force
    $tpl | Add-Member -NotePropertyName "rom_path" -NotePropertyValue $resolvedRomPath -Force
    $tpl | Add-Member -NotePropertyName "command" -NotePropertyValue @($fefwExe) -Force
    if (-not $tpl.env) {
        $tpl | Add-Member -NotePropertyName "env" -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $tpl.env | Add-Member -NotePropertyName "NXRT_FS_SAVE_DIR" -NotePropertyValue $globalSaveDir -Force
    [System.IO.File]::WriteAllText($launchConfigPath, ($tpl | ConvertTo-Json -Depth 10), $utf8NoBom)
}

$iconSrc = $IconPath
if (-not $iconSrc -or -not (Test-Path -LiteralPath $iconSrc)) {
    $iconSrc = Join-Path $source "game_icon.ico"
}
$iconDst = Join-Path $Dir "game_icon.ico"
if (Test-Path -LiteralPath $iconSrc) {
    Copy-Item -LiteralPath $iconSrc -Destination $iconDst -Force
}
$pngSrc = Join-Path $source "game_icon.png"
$pngDst = Join-Path $Dir "game_icon.png"
if (Test-Path -LiteralPath $pngSrc) {
    Copy-Item -LiteralPath $pngSrc -Destination $pngDst -Force
}

$runner = Join-Path $Dir "fefw.cmd"
if ($isLauncherExe -and (Test-Path -LiteralPath $launcherUiDst) -and (Test-Path -LiteralPath $launchConfigPath)) {
    $cmdText = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0game_launcher.exe" --config "%~dp0launch_options.json" %*
"@
} else {
    $cmdText = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0fefw.exe" %*
"@
}
[System.IO.File]::WriteAllText($runner, $cmdText, $utf8NoBom)

$safeShortcutName = ($ShortcutName -replace '[<>:"/\\|?*]+', ' -').Trim()
if (-not $safeShortcutName) { $safeShortcutName = "Fire Emblem Fortune's Weave" }

$startMenuPrograms = [Environment]::GetFolderPath("Programs")
$shortcutDirs = @((Join-Path $startMenuPrograms $safeShortcutName))
if (-not $NoDesktopShortcut) {
    $shortcutDirs += [Environment]::GetFolderPath("Desktop")
}

$wsh = New-Object -ComObject WScript.Shell
foreach ($sdir in $shortcutDirs) {
    New-Item -ItemType Directory -Force -Path $sdir | Out-Null
    $linkPath = Join-Path $sdir "$safeShortcutName.lnk"
    $link = $wsh.CreateShortcut($linkPath)
    $link.TargetPath = $runner
    $link.WorkingDirectory = $Dir
    $link.Description = "$safeShortcutName - Native PC port (F11 Fullscreen)"
    if (Test-Path -LiteralPath $iconDst) { $link.IconLocation = "$iconDst,0" }
    $link.Save()
}

# ---- 7. Clean up temporary build files ----
Say "cleaning up temporary build files in $Dir..."
if (Test-Path -LiteralPath $workRoot) {
    Remove-Item -Recurse -Force $workRoot -ErrorAction SilentlyContinue
}

# ---- 8. Register in Windows Installed Apps + create uninstaller (never deletes ROM or saves) ----
$uninstKeyName = "Switch2Recomp_" + ($safeShortcutName -replace '[^A-Za-z0-9_-]', '_')
$uninstRegPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$uninstKeyName"
$uninstPs1 = Join-Path $Dir "uninstall.ps1"
$desktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "$safeShortcutName.lnk"
$startMenuDir = Join-Path ([Environment]::GetFolderPath("Programs")) $safeShortcutName
$eAcute = [char]0x00E9
$uninstScript = @"
Add-Type -AssemblyName System.Windows.Forms
`$res = [System.Windows.Forms.MessageBox]::Show(
    "Voulez-vous d${eAcute}sinstaller $safeShortcutName ?``n``nLe dossier d'installation et les raccourcis seront supprim${eAcute}s.``nVotre fichier ROM (.nup) et vos sauvegardes ne seront PAS supprim${eAcute}s.",
    "D${eAcute}sinstaller $safeShortcutName",
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Question
)
if (`$res -ne [System.Windows.Forms.DialogResult]::Yes) { exit 0 }
if (Test-Path -LiteralPath "$desktopLnk") { Remove-Item -LiteralPath "$desktopLnk" -Force -ErrorAction SilentlyContinue }
if (Test-Path -LiteralPath "$startMenuDir") { Remove-Item -LiteralPath "$startMenuDir" -Recurse -Force -ErrorAction SilentlyContinue }
if (Test-Path -LiteralPath "$uninstRegPath") {
    Remove-Item -LiteralPath "$uninstRegPath" -Recurse -Force -ErrorAction SilentlyContinue
}
Set-Location `$env:TEMP
Start-Process -FilePath "cmd.exe" -ArgumentList "/c timeout /t 1 /nobreak >nul & rmdir /s /q ``"$Dir``"" -WindowStyle Hidden
"@
[System.IO.File]::WriteAllText($uninstPs1, $uninstScript, (New-Object System.Text.UTF8Encoding($true)))

$sizeKb = [int](((Get-ChildItem -LiteralPath $Dir -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum) / 1KB)
New-Item -Path $uninstRegPath -Force | Out-Null
Set-ItemProperty -Path $uninstRegPath -Name "DisplayName" -Value $safeShortcutName
Set-ItemProperty -Path $uninstRegPath -Name "Publisher" -Value "Switch2Recomp (NUP Studio)"
Set-ItemProperty -Path $uninstRegPath -Name "InstallLocation" -Value $Dir
if (Test-Path -LiteralPath $iconDst) { Set-ItemProperty -Path $uninstRegPath -Name "DisplayIcon" -Value "$iconDst,0" }
$uninstCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$uninstPs1`""
Set-ItemProperty -Path $uninstRegPath -Name "UninstallString" -Value $uninstCmd
Set-ItemProperty -Path $uninstRegPath -Name "QuietUninstallString" -Value $uninstCmd
Set-ItemProperty -Path $uninstRegPath -Name "NoModify" -Value 1 -Type DWord
Set-ItemProperty -Path $uninstRegPath -Name "NoRepair" -Value 1 -Type DWord
Set-ItemProperty -Path $uninstRegPath -Name "EstimatedSize" -Value $sizeKb -Type DWord

Say "installed. Start the game from the Start menu or the desktop shortcut (windowed 720p by default, F11 for fullscreen)."
