.PHONY: help install run dev test clean docker-build docker-up docker-down docker-logs backup

# Variables
PYTHON := python
PIP := pip
UVICORN := uvicorn
DOCKER_COMPOSE := docker-compose

help: ## Afficher l'aide
	@echo "Commandes disponibles :"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Installer les dépendances Python
	$(PIP) install -r requirements.txt
	playwright install chromium

run: ## Lancer l'application (production)
	$(UVICORN) main:app --host 0.0.0.0 --port 8000

dev: ## Lancer l'application en mode développement (hot reload)
	$(UVICORN) main:app --host 0.0.0.0 --port 8000 --reload

test: ## Lancer les tests (TODO: implémenter)
	@echo "Tests à implémenter"

clean: ## Nettoyer les fichiers temporaires
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.log" -delete
	rm -rf .pytest_cache .coverage htmlcov

docker-build: ## Construire l'image Docker
	$(DOCKER_COMPOSE) build

docker-up: ## Démarrer les services Docker
	$(DOCKER_COMPOSE) up -d

docker-down: ## Arrêter les services Docker
	$(DOCKER_COMPOSE) down

docker-logs: ## Afficher les logs Docker
	$(DOCKER_COMPOSE) logs -f

docker-restart: ## Redémarrer les services Docker
	$(DOCKER_COMPOSE) restart

docker-ps: ## Afficher l'état des conteneurs
	$(DOCKER_COMPOSE) ps

backup: ## Créer un backup de la base de données
	@mkdir -p backups
	@docker cp edt-backend:/app/data/edt.db backups/edt-backup-$$(date +%Y%m%d-%H%M%S).db
	@echo "Backup créé : backups/edt-backup-$$(date +%Y%m%d-%H%M%S).db"

deploy: ## Déployer en production (via deploy.sh)
	chmod +x deploy.sh
	./deploy.sh start

status: ## Afficher le statut des services
	./deploy.sh status

lint: ## Vérifier le code avec flake8
	$(PIP) install flake8
	flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics

format: ## Formater le code avec black
	$(PIP) install black
	black .

check-env: ## Vérifier la configuration .env
	@test -f .env || (echo "❌ Fichier .env manquant. Copier .env.example vers .env" && exit 1)
	@echo "✅ Fichier .env trouvé"

init: install check-env ## Installation complète (dépendances + vérifications)
	@echo "✅ Installation terminée"
