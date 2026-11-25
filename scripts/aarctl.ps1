<#
.SYNOPSIS
  基盤の起動・停止・状態確認（Windows の入口）。

.DESCRIPTION
  使い方: .\scripts\aarctl.ps1 start | stop [--all] | restart-worker | status | logs [temporal|worker|ui] [--lines N] | doctor | verify

  実際の処理は Python 側（aar.ctl）にあり、このスクリプトは呼び出すだけ。
    * Windows ネイティブ方式: リポジトリの .venv の Python で実行する。
    * WSL 方式: 環境変数 AAR_WSL_DISTRO にディストリビューション名を設定すると、その中の同じ処理を wsl.exe 経由で呼ぶ
      （リポジトリの場所は AAR_WSL_REPO。WSL の中のパスで指定する）。

  引数は配列のまま渡す。文字列を連結してコマンドを組み立てない（Invoke-Expression は使わない）。
  電源の設定、実行ポリシー、社内の設定は変更しない。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][ValidateSet('start', 'stop', 'restart-worker', 'status', 'logs', 'doctor', 'verify')][string]$Command = 'status',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

switch ($Command) {
    'doctor' { $aarArgs = @('doctor') }
    'verify' { $aarArgs = @('podman', 'verify') + $Rest }
    default  { $aarArgs = @('ctl', $Command) + $Rest }
}

if ($env:AAR_WSL_DISTRO) {
    if (-not $env:AAR_WSL_REPO) { throw 'AAR_WSL_REPO（WSL の中でのリポジトリの場所）を設定してください。' }
    # ディストリビューション名とパスは、wsl.exe の引数として渡す（シェルの文字列にしない）
    & wsl.exe --distribution $env:AAR_WSL_DISTRO --cd $env:AAR_WSL_REPO --exec .venv/bin/python -X utf8 -m aar.cli @aarArgs
    exit $LASTEXITCODE
}

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Python の仮想環境がありません: $python（docs/windows/setup.md を参照）" }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Push-Location -LiteralPath $root
try {
    & $python -X utf8 -m aar.cli @aarArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
