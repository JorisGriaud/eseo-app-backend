# Script de déploiement distant pour EDT ESEO Backend (Windows)
# Usage: .\deploy-remote.ps1 -Server "user@votre-serveur.com"

param(
    [Parameter(Mandatory=$true)]
    [string]$Server,

    [string]$RemoteDir = "/opt/edt-eseo"
)

Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Déploiement EDT ESEO Backend" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# Vérifier que Git Bash ou WSL est disponible pour SSH/SCP
$sshPath = Get-Command ssh -ErrorAction SilentlyContinue
if (-not $sshPath) {
    Write-Host "❌ SSH non trouvé. Installez Git Bash ou WSL." -ForegroundColor Red
    exit 1
}

Write-Host "🎯 Serveur cible: $Server" -ForegroundColor Green
Write-Host "📁 Répertoire distant: $RemoteDir" -ForegroundColor Green
Write-Host ""

# Étape 1: Créer l'archive des fichiers
Write-Host "[1/5] 📦 Création de l'archive..." -ForegroundColor Yellow

$excludeDirs = @(
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "data",
    "backups",
    "swag",
    ".vscode",
    ".idea"
)

$excludeFiles = @(
    "*.db",
    "*.pyc",
    "*.pyo",
    "*.log",
    ".env"
)

$archiveName = "edt-backend-deploy-$(Get-Date -Format 'yyyyMMdd-HHmmss').zip"

# Créer l'archive (nécessite PowerShell 5.0+)
$filesToInclude = Get-ChildItem -Path . -Recurse | Where-Object {
    $include = $true
    foreach ($exclude in $excludeDirs) {
        if ($_.FullName -like "*\$exclude\*") {
            $include = $false
            break
        }
    }
    foreach ($pattern in $excludeFiles) {
        if ($_.Name -like $pattern) {
            $include = $false
            break
        }
    }
    $include
}

# Utiliser 7-Zip ou la compression native de PowerShell
try {
    Compress-Archive -Path ./* -DestinationPath $archiveName -Force -CompressionLevel Optimal
    Write-Host "✅ Archive créée: $archiveName" -ForegroundColor Green
} catch {
    Write-Host "❌ Erreur lors de la création de l'archive" -ForegroundColor Red
    exit 1
}

# Étape 2: Créer le répertoire distant
Write-Host ""
Write-Host "[2/5] 📂 Création du répertoire distant..." -ForegroundColor Yellow
ssh $Server "sudo mkdir -p $RemoteDir && sudo chown `$USER:`$USER $RemoteDir"

# Étape 3: Transférer l'archive
Write-Host ""
Write-Host "[3/5] 📤 Transfert de l'archive..." -ForegroundColor Yellow
scp $archiveName "${Server}:${RemoteDir}/"

# Étape 4: Décompresser et configurer sur le serveur
Write-Host ""
Write-Host "[4/5] 📦 Décompression et configuration..." -ForegroundColor Yellow

$remoteCommands = @"
cd $RemoteDir
unzip -o $archiveName
rm $archiveName
if [ ! -f .env ]; then
    cp .env.example .env
    echo '⚠️  Fichier .env créé - PENSEZ À LE CONFIGURER !'
fi
chmod +x deploy.sh
"@

ssh $Server $remoteCommands

# Étape 5: Déployer
Write-Host ""
Write-Host "[5/5] 🚀 Déploiement de l'application..." -ForegroundColor Yellow
ssh $Server "cd $RemoteDir && sudo ./deploy.sh update"

# Nettoyage local
Remove-Item $archiveName -Force

Write-Host ""
Write-Host "═══════════════════════════════════════════" -ForegroundColor Green
Write-Host "  ✅ Déploiement terminé avec succès !" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "📊 Commandes utiles:" -ForegroundColor Cyan
Write-Host "  ssh $Server 'cd $RemoteDir && sudo ./deploy.sh status'" -ForegroundColor White
Write-Host "  ssh $Server 'cd $RemoteDir && sudo ./deploy.sh logs'" -ForegroundColor White
Write-Host "  ssh $Server 'cd $RemoteDir && sudo ./deploy.sh backup'" -ForegroundColor White
Write-Host ""
