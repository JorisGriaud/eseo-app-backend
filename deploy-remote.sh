#!/bin/bash

# Script de déploiement distant pour EDT ESEO Backend
# Usage: ./deploy-remote.sh [SERVER_USER@SERVER_IP]

set -e

# Couleurs
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Configuration
SERVER="${1:-user@votre-serveur.com}"
REMOTE_DIR="/opt/edt-eseo"
EXCLUDE_FILE=".deployignore"

# Vérifications
if [ -z "$SERVER" ]; then
    echo -e "${RED}[ERROR]${NC} Usage: $0 SERVER_USER@SERVER_IP"
    exit 1
fi

echo -e "${GREEN}[INFO]${NC} Déploiement vers: $SERVER:$REMOTE_DIR"

# Créer le fichier .deployignore s'il n'existe pas
if [ ! -f "$EXCLUDE_FILE" ]; then
    cat > "$EXCLUDE_FILE" << 'EOF'
.git/
__pycache__/
*.pyc
*.pyo
*.db
data/
backups/
swag/
venv/
.venv/
.env
.vscode/
.idea/
*.log
EOF
fi

# 1. Créer le répertoire distant si nécessaire
echo -e "${GREEN}[STEP 1/5]${NC} Création du répertoire distant..."
ssh "$SERVER" "sudo mkdir -p $REMOTE_DIR && sudo chown \$USER:\$USER $REMOTE_DIR"

# 2. Synchroniser les fichiers avec rsync
echo -e "${GREEN}[STEP 2/5]${NC} Synchronisation des fichiers..."
rsync -avz --delete \
    --exclude-from="$EXCLUDE_FILE" \
    ./ "$SERVER:$REMOTE_DIR/"

# 3. Copier .env si nécessaire
echo -e "${GREEN}[STEP 3/5]${NC} Configuration de l'environnement..."
ssh "$SERVER" "cd $REMOTE_DIR && if [ ! -f .env ]; then cp .env.example .env; echo '⚠️  Fichier .env créé - CONFIGUREZ-LE !'; fi"

# 4. Rendre les scripts exécutables
echo -e "${GREEN}[STEP 4/5]${NC} Configuration des permissions..."
ssh "$SERVER" "cd $REMOTE_DIR && chmod +x deploy.sh"

# 5. Déployer
echo -e "${GREEN}[STEP 5/5]${NC} Déploiement de l'application..."
ssh "$SERVER" "cd $REMOTE_DIR && sudo ./deploy.sh update"

echo ""
echo -e "${GREEN}✅ Déploiement terminé !${NC}"
echo ""
echo "Commandes utiles :"
echo "  ssh $SERVER 'cd $REMOTE_DIR && sudo ./deploy.sh status'"
echo "  ssh $SERVER 'cd $REMOTE_DIR && sudo ./deploy.sh logs'"
echo "  ssh $SERVER 'cd $REMOTE_DIR && sudo ./deploy.sh backup'"
