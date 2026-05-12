# Deploy bot-observability stack to server
param(
    [string]$DeployHost = $env:DEPLOY_HOST,
    [string]$DeployUser = "deployer",
    [string]$DeployPath = "/opt/bot-observability",
    [string]$SshKeyPath = "$env:USERPROFILE\.ssh\deploy_key"
)

if (-not $DeployHost) {
    Write-Error "DEPLOY_HOST environment variable not set"
    exit 1
}

Write-Host "Deploying bot-observability to $DeployUser@$DeployHost`:$DeployPath"

# Create tar bundle
Write-Host "Creating deployment bundle..."
$tarFile = "bot-observability.tar.gz"
git ls-files | tar -czf $tarFile --files-from -

if (-not (Test-Path $tarFile)) {
    Write-Error "Failed to create tar bundle"
    exit 1
}

Write-Host "Uploading bundle..."
scp -i $SshKeyPath -o StrictHostKeyChecking=no $tarFile "$DeployUser@$DeployHost`:/tmp/"

Write-Host "Extracting and restarting services..."
ssh -i $SshKeyPath -o StrictHostKeyChecking=no $DeployUser@$DeployHost @"
    sudo mkdir -p $DeployPath
    sudo tar -xzf /tmp/$tarFile -C $DeployPath
    cd $DeployPath
    docker compose down
    docker compose up -d
    sleep 5
    docker compose ps
"@

Remove-Item $tarFile
Write-Host "Deployment complete!"
