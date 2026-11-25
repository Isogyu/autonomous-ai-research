<#
.SYNOPSIS
  実行方式（WSL2 か、Windows ネイティブ＋Podman の仮想環境か）を決めるための、環境の事実を集める。

.DESCRIPTION
  読み取りだけを行う。設定の変更、機能の有効化、インストール、管理者への昇格はしない。
  「技術的に使えるか」だけを報告する。「社内で許可されているか」は、この結果からは分からない（情報システム部門に確認する）。
  結果は標準出力に出す。利用者名・ホスト名・プロキシの URL は、共有する前に確認して伏せる。

  使い方: powershell -NoProfile -File .\scripts\windows\Probe-Environment.ps1 > probe.txt
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

function Section([string]$Title) { Write-Output ''; Write-Output "== $Title ==" }
function Try-Run([string]$Label, [scriptblock]$Block) {
    try { $r = & $Block 2>&1 | Out-String; Write-Output ("{0}: {1}" -f $Label, $r.Trim()) }
    catch { Write-Output ("{0}: 取得できない（{1}）" -f $Label, $_.Exception.Message) }
}
function Tool([string]$Name, [string[]]$VersionArgs) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { Write-Output "${Name}: 見つからない"; return }
    # 実行ファイルの種類を確かめる（.cmd のランチャーは、基盤からは直接起動しない）
    Write-Output ("{0}: {1}（種類 {2}）" -f $Name, $cmd.Source, $cmd.CommandType)
    Try-Run "  版" { & $cmd.Source @VersionArgs }
}

Section 'Windows'
Try-Run '版' { (Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture | Format-List | Out-String) }
Try-Run 'CPU' { (Get-CimInstance Win32_Processor | Select-Object -First 1 Name, NumberOfCores, NumberOfLogicalProcessors, VirtualizationFirmwareEnabled, SecondLevelAddressTranslationExtensions | Format-List | Out-String) }
Try-Run 'メモリ（GB）' { [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1) }
Try-Run 'ディスクの空き（GB）' { Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { '{0} {1}' -f $_.DeviceID, [math]::Round($_.FreeSpace / 1GB, 1) } }
Try-Run '管理者として実行中か' { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
Try-Run 'ハイパーバイザーの有無' { (Get-CimInstance Win32_ComputerSystem).HypervisorPresent }
Try-Run 'タイムゾーン' { (Get-TimeZone).Id }
Try-Run '既定の文字コード（ANSI / OEM）' { '{0} / {1}' -f [Text.Encoding]::Default.CodePage, [Console]::OutputEncoding.CodePage }
Try-Run '長いパスの許可' { (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled }

Section 'WSL / Hyper-V（導入の状況。許可の有無は別に確認する）'
Try-Run 'wsl --status' { & wsl.exe --status }
Try-Run 'wsl --version' { & wsl.exe --version }
Try-Run 'wsl --list --verbose' { & wsl.exe --list --verbose }
Try-Run 'vmcompute（仮想化の基盤のサービス）' { (Get-Service vmcompute -ErrorAction Stop).Status }
Try-Run 'WSL のポリシー（HKLM\SOFTWARE\Policies\Microsoft\Windows\WSL）' { Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WSL' -ErrorAction Stop | Out-String }

Section 'Podman'
Tool 'podman' @('--version')
Try-Run 'podman machine list' { & podman machine list }
Try-Run 'podman system connection list' { & podman system connection list }
Try-Run 'podman info（rootless、接続先、cgroups）' { & podman info --format 'version={{.Version.Version}} rootless={{.Host.Security.Rootless}} remote={{.Host.ServiceIsRemote}} cgroups={{.Host.CgroupsVersion}} os={{.Host.OS}} arch={{.Host.Arch}}' }
Try-Run 'podman machine inspect（provider、rootful）' { & podman machine inspect --format 'provider={{.VMType}} rootful={{.Rootful}} cpus={{.Resources.CPUs}} memory={{.Resources.Memory}}' }
Try-Run 'podman compose の provider（基盤は Compose を使わない。参考）' { $env:PODMAN_COMPOSE_WARNING_LOGS = 'true'; & podman compose version }
Try-Run 'レジストリの設定（registries.conf の場所は環境による）' { & podman info --format '{{range $k, $v := .Registries}}{{$k}} {{end}}' }

Section '開発の道具'
Tool 'python' @('--version')
Tool 'py' @('--list')
Tool 'git' @('--version')
Try-Run 'git core.autocrlf / core.longpaths' { '{0} / {1}' -f (& git config --get core.autocrlf), (& git config --get core.longpaths) }
Tool 'gh' @('--version')
Tool 'temporal' @('--version')
Tool 'node' @('--version')
Tool 'claude' @('--version')
Tool 'codex' @('--version')

Section 'ネットワーク（値は伏せ、設定の有無だけを出す）'
foreach ($name in 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS', 'PIP_INDEX_URL', 'PIP_CERT') {
    $v = [Environment]::GetEnvironmentVariable($name)
    Write-Output ("{0}: {1}" -f $name, $(if ($v) { '設定あり' } else { '未設定' }))
}
Try-Run 'WinHTTP のプロキシ' { & netsh winhttp show proxy }

Section '電源（読み取りだけ）'
Try-Run '使えるスリープの状態' { & powercfg /a }
Try-Run '電源のイベントを読めるか（Kernel-Power 42/107/506/507、直近 5 件）' { & wevtutil qe System "/q:*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=42 or EventID=107 or EventID=506 or EventID=507)]]" /c:5 /rd:true /f:text }
