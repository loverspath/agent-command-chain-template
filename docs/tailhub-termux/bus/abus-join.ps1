# abus v2 원클릭 노드 구성 (Windows). 멱등. 사용: powershell -ExecutionPolicy Bypass -File abus-join.ps1 [-Label main] [-Mode hybrid] [-Peers a,b] [-DryRun]
param([string]$Label="main",[string]$Mode="hybrid",
 [string]$Peers="cat-food,msi,mother-goose,claw8,z-fold8,z-fold5,codex-environment",
 [string[]]$Urls=@("ws://100.87.35.41:8765","ws://100.124.66.37:8765"),
 [string]$Repo="$env:USERPROFILE\agent-command-chain-template",[switch]$DryRun)
$ErrorActionPreference="Stop"
function Run($s){ Write-Host "+ $s"; if(-not $DryRun){ & ([scriptblock]::Create($s)) } }
$cfgDir="$env:USERPROFILE\.config\agent-bus"; New-Item -ItemType Directory -Force $cfgDir | Out-Null
if(Test-Path "$Repo\.git"){ Run "git -C '$Repo' pull --ff-only" }
else { if(Test-Path $Repo){ Run "Rename-Item '$Repo' '$(Split-Path $Repo -Leaf).old-$(Get-Date -f yyyyMMddHHmm)'" }
  Run "git clone https://github.com/loverspath/agent-command-chain-template.git '$Repo'" }
if(-not $DryRun){
  $p="$cfgDir\config.json"; $c=@{}
  if(Test-Path $p){ Copy-Item $p "$p.bak-$(Get-Date -f yyyyMMddHHmm)"; (Get-Content $p -Raw | ConvertFrom-Json).PSObject.Properties | % { $c[$_.Name]=$_.Value } }
  $c["urls"]=$Urls; if(-not $c.ContainsKey("url")){$c["url"]=$Urls[0]}; $c["label"]=$Label; $c["peers"]=$Peers
  $c | ConvertTo-Json -Depth 5 | Set-Content $p -Encoding UTF8
  Set-Content "$cfgDir\mode" $Mode -Encoding ASCII
}
# 감독자: 로그온 트리거 작업 스케줄러(재시도 3회). 관리자 권한이 없으면 현재 세션에서 숨김 기동만.
$py=(Get-Command python).Source
$args="-u `"$Repo\docs\tailhub-termux\bus\node.py`" run --label $Label --mode $Mode --peers $Peers"
$task="Abus-Node-$Label"
if(-not (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)){
  Run "Register-ScheduledTask -TaskName '$task' -Action (New-ScheduledTaskAction -Execute '$py' -Argument '$args') -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings (New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)) -Force | Out-Null"
}
if(-not (Get-CimInstance Win32_Process | ? { $_.CommandLine -match "node.py run --label $Label" })){ Run "Start-Process '$py' -ArgumentList '$args' -WindowStyle Hidden" }
else { Write-Host "node($Label) 이미 실행 중 (코드 갱신 반영은 running 0 확인 후 해당 PID만 종료)" }
if(-not $DryRun){ Start-Sleep 8; $env:PYTHONPATH="$Repo\bus"; python -m agent_bus.cli doctor --json | python -c "import json,sys;d=json.load(sys.stdin);print('mode',d.get('mode'),'all_hubs_ok',d.get('all_hubs_ok'))" }
