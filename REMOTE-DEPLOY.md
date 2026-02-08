# Guide de Déploiement Distant

Guide pour déployer l'EDT ESEO Backend depuis votre PC Windows vers un serveur Linux distant.

## 🎯 Prérequis

### Sur votre PC Windows

- ✅ Git Bash ou WSL installé (pour SSH/SCP)
- ✅ Accès SSH au serveur distant
- ✅ Clé SSH configurée (recommandé)

### Sur le serveur distant

- ✅ Docker installé
- ✅ Docker Compose installé
- ✅ Accès sudo

## 📋 Méthodes de déploiement

---

## Méthode 1 : Git (RECOMMANDÉ) 🌟

### Configuration initiale (une seule fois)

**Sur votre PC :**

```powershell
# 1. Initialiser Git (si pas déjà fait)
cd C:\Users\joris\Documents\Projet\edt_app_backend
git init
git add .
git commit -m "Initial commit"

# 2. Créer un repo sur GitHub/GitLab
# Aller sur https://github.com/new

# 3. Ajouter le remote et pousser
git remote add origin https://github.com/VOTRE_USER/edt-backend.git
git branch -M main
git push -u origin main
```

**Sur le serveur :**

```bash
# Se connecter en SSH
ssh user@votre-serveur.com

# Installer Docker si nécessaire
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
# Déconnexion/reconnexion pour appliquer le groupe

# Cloner le projet
cd /opt
sudo git clone https://github.com/VOTRE_USER/edt-backend.git edt-eseo
sudo chown -R $USER:$USER edt-eseo
cd edt-eseo

# Configurer
cp .env.example .env
nano .env  # Modifier DOMAIN, EMAIL, JWT_SECRET_KEY

# Déployer
chmod +x deploy.sh
./deploy.sh start
```

### Mises à jour (après chaque modification)

**Sur votre PC :**

```powershell
# Commiter et pousser les changements
git add .
git commit -m "Description des changements"
git push
```

**Sur le serveur :**

```bash
ssh user@votre-serveur.com
cd /opt/edt-eseo
git pull
./deploy.sh update
```

### ✅ Avantages
- Simple et standard
- Historique des versions
- Facilite le travail en équipe
- Rollback facile

---

## Méthode 2 : Script automatisé (PowerShell) 🚀

### Utilisation

**Sur votre PC Windows (PowerShell) :**

```powershell
# Naviguer vers le projet
cd C:\Users\joris\Documents\Projet\edt_app_backend

# Première utilisation - Configuration du serveur
.\deploy-remote.ps1 -Server "user@votre-serveur.com"

# L'utilisateur devra ensuite configurer .env sur le serveur
ssh user@votre-serveur.com
cd /opt/edt-eseo
nano .env  # Configurer DOMAIN, EMAIL, JWT_SECRET_KEY
./deploy.sh start

# Mises à jour ultérieures
.\deploy-remote.ps1 -Server "user@votre-serveur.com"
```

### Options du script

```powershell
# Déployer vers un serveur spécifique
.\deploy-remote.ps1 -Server "admin@production.example.com"

# Spécifier un répertoire distant différent
.\deploy-remote.ps1 -Server "user@server.com" -RemoteDir "/home/user/edt"
```

### ✅ Avantages
- Déploiement en une commande
- Transfert rapide (archive ZIP)
- Automatisé

---

## Méthode 3 : Script Bash (Git Bash/WSL) 🐧

**Dans Git Bash ou WSL :**

```bash
# Rendre le script exécutable
chmod +x deploy-remote.sh

# Déployer
./deploy-remote.sh user@votre-serveur.com

# Ou avec rsync pour synchronisation incrémentale
rsync -avz --delete \
  --exclude='.git' --exclude='*.db' --exclude='data/' \
  ./ user@votre-serveur.com:/opt/edt-eseo/

ssh user@votre-serveur.com 'cd /opt/edt-eseo && ./deploy.sh update'
```

---

## Méthode 4 : SCP manuel 📦

**Créer une archive :**

```powershell
# Sur Windows PowerShell
Compress-Archive -Path * -DestinationPath edt-backend.zip

# Transférer avec SCP (Git Bash)
scp edt-backend.zip user@votre-serveur.com:/opt/

# Sur le serveur
ssh user@votre-serveur.com
cd /opt
unzip edt-backend.zip -d edt-eseo
cd edt-eseo
cp .env.example .env
nano .env
chmod +x deploy.sh
./deploy.sh start
```

---

## 🔐 Configuration SSH recommandée

### Créer une clé SSH (si vous n'en avez pas)

**Sur Windows (PowerShell) :**

```powershell
# Générer la clé
ssh-keygen -t ed25519 -C "votre@email.com"

# Copier la clé publique vers le serveur
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh user@serveur.com "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

### Simplifier la connexion SSH

**Créer/éditer `~/.ssh/config` :**

```
Host edt-prod
    HostName votre-serveur.com
    User votre-user
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
```

**Utilisation :**

```bash
# Au lieu de: ssh user@votre-serveur.com
ssh edt-prod

# Avec les scripts
.\deploy-remote.ps1 -Server "edt-prod"
```

---

## 🔄 Workflow recommandé

### 1. Développement local

```powershell
# Faire vos modifications localement
# Tester avec:
python -m uvicorn main:app --reload
```

### 2. Commit et push

```powershell
git add .
git commit -m "feat: description de la fonctionnalité"
git push
```

### 3. Déployer

```bash
# SSH sur le serveur
ssh edt-prod
cd /opt/edt-eseo
git pull
./deploy.sh update
```

**Ou en une seule commande locale :**

```bash
ssh edt-prod 'cd /opt/edt-eseo && git pull && ./deploy.sh update'
```

---

## 📊 Commandes utiles après déploiement

```bash
# Vérifier l'état
ssh edt-prod './deploy.sh status'

# Voir les logs
ssh edt-prod './deploy.sh logs'

# Créer un backup
ssh edt-prod './deploy.sh backup'

# Redémarrer
ssh edt-prod './deploy.sh restart'
```

---

## 🆘 Dépannage

### Problème : Permission denied lors du SSH

```powershell
# Vérifier les permissions de la clé privée (Git Bash)
chmod 600 ~/.ssh/id_ed25519

# Ou créer une nouvelle clé
ssh-keygen -t ed25519 -C "email@example.com"
```

### Problème : Docker permission denied

```bash
# Sur le serveur
sudo usermod -aG docker $USER
# Déconnexion/reconnexion nécessaire
```

### Problème : Port déjà utilisé

```bash
# Vérifier les ports utilisés
ssh edt-prod 'sudo netstat -tulpn | grep -E ":(80|443|9080)"'

# Modifier le port dans docker-compose.yml si nécessaire
```

### Problème : .env non configuré

```bash
# SSH sur le serveur
ssh edt-prod
cd /opt/edt-eseo
nano .env

# Vérifier la configuration
cat .env
```

---

## 🎯 Checklist de déploiement

- [ ] Serveur accessible via SSH
- [ ] Docker installé sur le serveur
- [ ] Clé SSH configurée (recommandé)
- [ ] Enregistrement DNS configuré (CNAME ou A)
- [ ] Ports 80 et 443 ouverts sur le serveur
- [ ] `.env` configuré avec les bonnes valeurs
- [ ] Premier déploiement réussi
- [ ] Health check OK : `curl https://edt-api.votredomaine.com/health`

---

## 📈 CI/CD (Optionnel - Avancé)

Pour automatiser complètement, créer `.github/workflows/deploy.yml` :

```yaml
name: Deploy to Production

on:
  push:
    branches: [ main ]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Deploy to server
        uses: appleboy/ssh-action@master
        with:
          host: ${{ secrets.SERVER_HOST }}
          username: ${{ secrets.SERVER_USER }}
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd /opt/edt-eseo
            git pull
            ./deploy.sh update
```

**Configurer les secrets GitHub :**
- `SERVER_HOST` : votre-serveur.com
- `SERVER_USER` : votre-user
- `SSH_PRIVATE_KEY` : contenu de votre clé privée SSH

---

## 🎓 Résumé

| Méthode | Complexité | Vitesse | Recommandation |
|---------|-----------|---------|----------------|
| Git | ⭐⭐ | ⭐⭐⭐ | ✅ **MEILLEUR** |
| Script PS1 | ⭐⭐⭐ | ⭐⭐ | 👍 Pratique |
| Script Bash | ⭐⭐⭐ | ⭐⭐ | 👍 Pratique |
| SCP manuel | ⭐ | ⭐ | ⚠️ Basique |
| CI/CD | ⭐⭐⭐⭐ | ⭐⭐⭐ | 🚀 Pro |

**Recommandation finale : Utilisez Git !** C'est la méthode la plus standard et la plus simple à long terme.
