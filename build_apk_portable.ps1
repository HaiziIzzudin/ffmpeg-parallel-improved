# Antigravity Portable Android Build Script
# This script sets up a local build environment containing JDK 17, Android SDK Tools, and Gradle 8.5, then builds the APK.
# Execution time: ~2-3 minutes (JDK and SDK are already downloaded; downloading Gradle will take ~30s).

$ErrorActionPreference = "Stop"

$workspace = Get-Location
$build_env = Join-Path $workspace "android_build_env"
$jdk_dest = Join-Path $build_env "jdk"
$sdk_dest = Join-Path $build_env "sdk"
$gradle_dest = Join-Path $build_env "gradle"
$cmdline_tools_dest = Join-Path $sdk_dest "cmdline-tools"

# Create directories
New-Item -ItemType Directory -Path $build_env -Force | Out-Null
New-Item -ItemType Directory -Path $jdk_dest -Force | Out-Null
New-Item -ItemType Directory -Path $sdk_dest -Force | Out-Null
New-Item -ItemType Directory -Path $gradle_dest -Force | Out-Null

# --- 1. Download & Extract OpenJDK 17 ---
$jdk_zip = Join-Path $build_env "openjdk17.zip"
if (-not (Test-Path $jdk_zip)) {
    Write-Host "Downloading OpenJDK 17 (Eclipse Temurin)..." -ForegroundColor Cyan
    $jdk_url = "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse?project=jdk"
    Invoke-WebRequest -Uri $jdk_url -OutFile $jdk_zip
}

$jdk_extracted = Get-ChildItem $jdk_dest -Directory
if ($jdk_extracted.Count -eq 0) {
    Write-Host "Extracting OpenJDK 17..." -ForegroundColor Cyan
    Expand-Archive -Path $jdk_zip -DestinationPath $jdk_dest -Force
    $jdk_extracted = Get-ChildItem $jdk_dest -Directory
}
$jdk_path = $jdk_extracted[0].FullName
Write-Host "JDK configured at: $jdk_path" -ForegroundColor Green

# --- 2. Download & Extract Android Command-Line Tools ---
$sdk_zip = Join-Path $build_env "cmdline-tools.zip"
if (-not (Test-Path $sdk_zip)) {
    Write-Host "Downloading Android SDK Commandline Tools..." -ForegroundColor Cyan
    $sdk_url = "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
    Invoke-WebRequest -Uri $sdk_url -OutFile $sdk_zip
}

$latest_tool_bin = Join-Path $cmdline_tools_dest "latest\bin\sdkmanager.bat"
if (-not (Test-Path $latest_tool_bin)) {
    Write-Host "Extracting Commandline Tools..." -ForegroundColor Cyan
    $tmp_extract = Join-Path $cmdline_tools_dest "tmp"
    New-Item -ItemType Directory -Path $tmp_extract -Force | Out-Null
    Expand-Archive -Path $sdk_zip -DestinationPath $tmp_extract -Force
    
    # Android SDK Manager expects cmdline-tools to be placed in: cmdline-tools/latest/bin/...
    $latest_dir = Join-Path $cmdline_tools_dest "latest"
    New-Item -ItemType Directory -Path $latest_dir -Force | Out-Null
    
    # Move files from tmp/cmdline-tools/* to latest/
    Move-Item -Path (Join-Path $tmp_extract "cmdline-tools\*") -Destination $latest_dir -Force
    Remove-Item -Path $tmp_extract -Recurse -Force
}
Write-Host "Android Commandline Tools configured." -ForegroundColor Green

# --- 3. Download & Extract Gradle 8.5 ---
$gradle_zip = Join-Path $build_env "gradle.zip"
if (-not (Test-Path $gradle_zip)) {
    Write-Host "Downloading Gradle 8.5..." -ForegroundColor Cyan
    $gradle_url = "https://services.gradle.org/distributions/gradle-8.5-bin.zip"
    Invoke-WebRequest -Uri $gradle_url -OutFile $gradle_zip
}

$gradle_extracted = Get-ChildItem $gradle_dest -Directory
if ($gradle_extracted.Count -eq 0) {
    Write-Host "Extracting Gradle 8.5..." -ForegroundColor Cyan
    Expand-Archive -Path $gradle_zip -DestinationPath $gradle_dest -Force
    $gradle_extracted = Get-ChildItem $gradle_dest -Directory
}
$gradle_bin = Join-Path $gradle_extracted[0].FullName "bin\gradle.bat"
Write-Host "Gradle configured at: $gradle_bin" -ForegroundColor Green

# --- 4. Set Up Local Environment Variables ---
$env:JAVA_HOME = $jdk_path
$env:ANDROID_HOME = $sdk_dest
$env:Path = "$env:JAVA_HOME\bin;$cmdline_tools_dest\latest\bin;$env:Path"

Write-Host "JAVA_HOME set to: $env:JAVA_HOME" -ForegroundColor Gray
Write-Host "ANDROID_HOME set to: $env:ANDROID_HOME" -ForegroundColor Gray

# --- 5. Accept Android SDK Licenses ---
Write-Host "Accepting Android SDK licenses..." -ForegroundColor Cyan
# Pipe answers to license prompts automatically
$acceptAnswers = , "y" * 30
$acceptAnswers | & "$cmdline_tools_dest\latest\bin\sdkmanager.bat" --licenses

# --- 6. Download Platform SDK 34 and Build Tools ---
Write-Host "Installing Android SDK Platform 34 and Build-Tools..." -ForegroundColor Cyan
& "$cmdline_tools_dest\latest\bin\sdkmanager.bat" --install "platforms;android-34" "build-tools;34.0.0"

# --- 7. Build the Spoke Android APK ---
Write-Host "Building Spoke APK..." -ForegroundColor Cyan
Push-Location (Join-Path $workspace "android_spoke")
try {
    # Run portable Gradle assembleDebug using our local JDK & Android SDK environment
    & $gradle_bin assembleDebug
    
    $apk_path = Join-Path $workspace "android_spoke\app\build\outputs\apk\debug\app-debug.apk"
    if (Test-Path $apk_path) {
        Write-Host ""
        Write-Host "==========================================" -ForegroundColor Green
        Write-Host "SUCCESS! APK successfully built." -ForegroundColor Green
        Write-Host "Target location:" -ForegroundColor Green
        Write-Host $apk_path -ForegroundColor Yellow
        Write-Host "==========================================" -ForegroundColor Green
    } else {
        Write-Error "Build finished but APK was not found at target location."
    }
}
finally {
    Pop-Location
}
