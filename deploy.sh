#!/bin/bash

# Script de déploiement EDT ESEO Backend
# Usage: ./deploy.sh [start|stop|restart|logs|backup]

set -e

# Couleurs pour les logs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Fonctions utilitaires
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Vérifier que docker compose est installé
check_dependencies() {
    if ! command -v docker compose &> /dev/null; then
        log_error "docker compose n'est pas installé"
        exit 1
    fi
}

# Vérifier que le fichier .env existe
check_env() {
    if [ ! -f .env ]; then
        log_warn "Fichier .env non trouvé"
        log_info "Copie de .env.example vers .env"
        cp .env.example .env
        log_warn "⚠️  Veuillez configurer le fichier .env avec vos valeurs"
        exit 1
    fi
}

# Démarrer les services
start() {
    log_info "Démarrage des services EDT ESEO..."
    check_dependencies
    check_env

    docker compose up -d --build

    log_info "Services démarrés !"
    log_info "Vérification de l'état..."
    sleep 5
    docker compose ps

    log_info ""
    log_info "📊 Commandes utiles :"
    log_info "  - Logs backend: docker compose logs -f edt-backend"
    log_info "  - Logs SWAG: docker compose logs -f swag"
    log_info "  - Health check: curl https://edt-api.votredomaine.com/health"
}

# Arrêter les services
stop() {
    log_info "Arrêt des services EDT ESEO..."
    docker compose down
    log_info "Services arrêtés !"
}

# Redémarrer les services
restart() {
    log_info "Redémarrage des services EDT ESEO..."
    stop
    sleep 2
    start
}

# Afficher les logs
logs() {
    SERVICE=${1:-edt-backend}
    log_info "Affichage des logs de $SERVICE..."
    docker compose logs -f --tail=100 "$SERVICE"
}

# Backup de la base de données
backup() {
    BACKUP_DIR="./backups"
    mkdir -p "$BACKUP_DIR"

    BACKUP_FILE="$BACKUP_DIR/edt-backup-$(date +%Y%m%d-%H%M%S).db"

    log_info "Création du backup de la base de données..."
    docker cp edt-backend:/app/data/edt.db "$BACKUP_FILE"

    if [ -f "$BACKUP_FILE" ]; then
        log_info "✅ Backup créé : $BACKUP_FILE"

        # Garder seulement les 10 derniers backups
        log_info "Nettoyage des anciens backups..."
        ls -t "$BACKUP_DIR"/edt-backup-*.db | tail -n +11 | xargs -r rm
        log_info "Backups conservés : $(ls -1 "$BACKUP_DIR"/edt-backup-*.db | wc -l)"
    else
        log_error "Échec de la création du backup"
        exit 1
    fi
}

# Restaurer un backup
restore() {
    BACKUP_FILE="$1"

    if [ -z "$BACKUP_FILE" ]; then
        log_error "Usage: ./deploy.sh restore <fichier_backup>"
        log_info "Backups disponibles :"
        ls -lh ./backups/edt-backup-*.db 2>/dev/null || log_warn "Aucun backup trouvé"
        exit 1
    fi

    if [ ! -f "$BACKUP_FILE" ]; then
        log_error "Fichier de backup non trouvé : $BACKUP_FILE"
        exit 1
    fi

    log_warn "⚠️  ATTENTION : Cette opération va remplacer la base de données actuelle"
    read -p "Continuer ? (y/N) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Arrêt du backend..."
        docker compose stop edt-backend

        log_info "Restauration du backup..."
        docker cp "$BACKUP_FILE" edt-backend:/app/data/edt.db

        log_info "Redémarrage du backend..."
        docker compose start edt-backend

        log_info "✅ Backup restauré avec succès"
    else
        log_info "Opération annulée"
    fi
}

# Mettre à jour le code
update() {
    log_info "Mise à jour du backend EDT ESEO..."

    # Backup avant mise à jour
    backup

    log_info "Reconstruction de l'image Docker..."
    docker compose build edt-backend

    log_info "Redémarrage du backend..."
    docker compose up -d edt-backend

    log_info "✅ Mise à jour terminée"
    log_info "Vérification de l'état..."
    sleep 3
    docker compose ps edt-backend
}

# Afficher l'état des services
status() {
    log_info "État des services EDT ESEO :"
    docker compose ps

    log_info ""
    log_info "Health check backend :"
    docker exec edt-backend curl -f http://localhost:8000/ 2>/dev/null && log_info "✅ Backend OK" || log_error "❌ Backend KO"
}

# Menu principal
case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    logs)
        logs "${2:-edt-backend}"
        ;;
    backup)
        backup
        ;;
    restore)
        restore "$2"
        ;;
    update)
        update
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|logs|backup|restore|update|status}"
        echo ""
        echo "Commandes :"
        echo "  start    - Démarrer les services"
        echo "  stop     - Arrêter les services"
        echo "  restart  - Redémarrer les services"
        echo "  logs     - Afficher les logs (défaut: edt-backend)"
        echo "  backup   - Créer un backup de la base de données"
        echo "  restore  - Restaurer un backup"
        echo "  update   - Mettre à jour le code et redémarrer"
        echo "  status   - Afficher l'état des services"
        echo ""
        echo "Exemples :"
        echo "  $0 start"
        echo "  $0 logs swag"
        echo "  $0 restore ./backups/edt-backup-20260208-120000.db"
        exit 1
        ;;
esac
