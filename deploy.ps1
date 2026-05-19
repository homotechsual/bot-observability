# Deploy bot-observability stack to server
#Requires -Version 7.0

param(
    [string]$DeployHost = $env:DEPLOY_HOST,
    [string]$DeployUser = "deployer",
    [string]$DeployPath = "/opt/bot-observability",
    [string]$SshKeyPath = "$env:USERPROFILE\.ssh\deploy_key"
)

# Stop on errors and report undefined variables
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

if (-not $DeployHost) {
    Write-Error "DEPLOY_HOST environment variable not set"
    exit 1
}

if (-not (Test-Path $SshKeyPath)) {
    Write-Error "SSH key not found at: $SshKeyPath"
    exit 1
}

Write-Host "Deploying bot-observability to $DeployUser@$DeployHost`:$DeployPath"

# Create tar bundle
Write-Host "`[Step 1`] Creating deployment bundle..."
$tarFile = "bot-observability.tar.gz"

try {
    git ls-files | tar -czf $tarFile --files-from -
    if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
} catch {
    Write-Error "Failed to create tar bundle: $_"
    exit 1
}

if (-not (Test-Path $tarFile)) {
    Write-Error "Failed to create tar bundle: file not found after creation"
    exit 1
}

$fileSizeMB = ((Get-Item $tarFile).Length / 1MB).ToString("F2")
Write-Host "[OK] Bundle created: $tarFile ($fileSizeMB MB)"

# Upload bundle
Write-Host "`[Step 2`] Uploading bundle to $DeployUser@$DeployHost..."
try {
    scp -i $SshKeyPath -o StrictHostKeyChecking=no $tarFile "$DeployUser@$DeployHost`:/tmp/"
    if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }
} catch {
    Write-Error "Failed to upload: $_"
    exit 1
}
Write-Host "[OK] Upload complete"

# Extract and restart
Write-Host "`[Step 3`] Extracting and restarting services..."
$remoteScript = @"
set -eu
echo '[DEBUG] Starting remote deployment...'
echo '[DEBUG] Creating deploy directory...'
sudo mkdir -p $DeployPath

echo '[DEBUG] Extracting tar bundle...'
sudo tar -xzf /tmp/$tarFile -C $DeployPath
if [ `$? -ne 0 ]; then
    echo '[ERROR] tar extraction failed'
    exit 1
fi

echo '[DEBUG] Changing to deploy directory...'
cd $DeployPath || exit 1

echo '[DEBUG] Stopping existing services...'
docker compose down --remove-orphans || true

echo '[DEBUG] Starting new services...'
docker compose up -d
if [ `$? -ne 0 ]; then
    echo '[ERROR] docker compose up failed'
    docker compose logs --tail 50
    exit 1
fi

echo '[DEBUG] Waiting for services to stabilize...'
sleep 5

echo '[DEBUG] Service status:'
docker compose ps

echo '[DEBUG] Remote deployment complete'
"@

try {
    ssh -i $SshKeyPath -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $DeployUser@$DeployHost $remoteScript
    if ($LASTEXITCODE -ne 0) { throw "Remote deployment failed with exit code $LASTEXITCODE. Check logs above." }
} catch {
    Write-Error "Failed during remote deployment: $_"
    
    # Try to get docker compose logs for debugging
    Write-Host "`n[Attempting to retrieve logs...]" -ForegroundColor Yellow
    ssh -i $SshKeyPath -o StrictHostKeyChecking=no $DeployUser@$DeployHost "docker compose -f $DeployPath/docker-compose.yml logs --tail 50 2>&1 || true" | Write-Host
    
    exit 1
}
Write-Host "[OK] Remote deployment complete" -ForegroundColor Green

# Cleanup
Write-Host "`[Step 4`] Cleaning up..."
try {
    Remove-Item $tarFile -ErrorAction Stop
    Write-Host "[OK] Local bundle cleaned"
} catch {
    Write-Warning "Could not remove local tar file: $_"
}

Write-Host "`n[SUCCESS] Deployment complete!" -ForegroundColor Green
