# Guide des Notifications Push Firebase

Ce guide explique comment configurer et utiliser les notifications push Firebase dans l'EDT ESEO Backend.

## 📊 Architecture actuelle

Le backend utilise l'**API FCM Legacy** (HTTP v1) pour envoyer des notifications.

```
Flutter App → Obtient FCM token
     ↓
Backend API → Stocke le token (POST /auth/register-device)
     ↓
APScheduler → Détecte changement d'emploi du temps
     ↓
send_firebase_notification() → Envoie via FCM Legacy API
     ↓
Firebase Cloud Messaging → Push vers l'appareil
```

## ✅ Ce qui est déjà implémenté

### 1. Endpoint d'enregistrement du token

**Endpoint** : `POST /auth/register-device`

**Headers** :
```
Authorization: Bearer <JWT_TOKEN>
Content-Type: application/json
```

**Body** :
```json
{
  "device_token": "le_token_FCM_du_telephone"
}
```

**Réponse** :
```json
{
  "message": "Device token registered successfully"
}
```

**Code** : [main.py:290-305](main.py)

### 2. Endpoint de déconnexion (supprime le token)

**Endpoint** : `DELETE /auth/logout`

**Headers** :
```
Authorization: Bearer <JWT_TOKEN>
```

**Code** : [main.py:308-323](main.py)

### 3. Fonction d'envoi de notifications

**Fonction** : `send_firebase_notification()`

**Paramètres** :
- `device_token` : Token FCM de l'appareil
- `title` : Titre de la notification
- `body` : Corps de la notification
- `data` : (optionnel) Données supplémentaires

**Code** : [scheduler.py:22-66](scheduler.py)

### 4. Intégration dans le scheduler

Le scheduler envoie automatiquement une notification quand l'emploi du temps change :

```python
if device_token:
    send_firebase_notification(
        device_token=device_token,
        title="Emploi du temps modifié",
        body="Votre emploi du temps a été mis à jour"
    )
```

**Code** : [scheduler.py:118-122](scheduler.py)

---

## 🔧 Configuration

### Étape 1 : Obtenir la FCM Server Key

1. Aller sur [Firebase Console](https://console.firebase.google.com/)
2. Sélectionner votre projet Flutter
3. **⚙️ Paramètres du projet** → **Cloud Messaging**
4. Onglet **"Cloud Messaging API (Legacy)"**
5. Copier la **Server Key** (commence par `AAAA...`)

⚠️ **Important** : L'API Legacy sera dépréciée. Voir section "Migration vers Firebase Admin SDK" ci-dessous.

### Étape 2 : Configurer la variable d'environnement

**Sur votre PC (développement)** :

Ajouter dans `.env` :
```bash
FCM_SERVER_KEY=AAAA...votre_server_key_ici
```

**Sur le serveur (production)** :

```bash
# Éditer .env sur le serveur
ssh user@votre-serveur.com
cd /opt/edt-eseo
nano .env
```

Ajouter :
```bash
FCM_SERVER_KEY=AAAA...votre_server_key_ici
```

Puis redémarrer :
```bash
./deploy.sh restart
```

### Étape 3 : Tester

**Depuis Flutter** :

```dart
// 1. Obtenir le token FCM
String? token = await FirebaseMessaging.instance.getToken();

// 2. Enregistrer le token sur le backend
final response = await http.post(
  Uri.parse('https://edt-api.votredomaine.com/auth/register-device'),
  headers: {
    'Authorization': 'Bearer $jwtToken',
    'Content-Type': 'application/json',
  },
  body: jsonEncode({'device_token': token}),
);
```

**Test manuel** :

```bash
# Tester l'envoi d'une notification (remplacer les valeurs)
curl -X POST https://fcm.googleapis.com/fcm/send \
  -H "Authorization: key=VOTRE_FCM_SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to": "TOKEN_FCM_DU_TELEPHONE",
    "notification": {
      "title": "Test notification",
      "body": "Ceci est un test",
      "sound": "default"
    },
    "priority": "high"
  }'
```

---

## 📋 Flux complet

### 1. Installation de l'app Flutter

```
User installe l'app
    ↓
Firebase SDK initialise
    ↓
FCM génère un device token
```

### 2. Login et enregistrement du token

```
User se connecte (POST /auth/login)
    ↓
Backend retourne JWT token
    ↓
Flutter obtient FCM device token
    ↓
Flutter enregistre le token (POST /auth/register-device)
    ↓
Backend stocke dans User.device_token
```

### 3. Synchronisation automatique et notifications

```
APScheduler tourne toutes les heures (7h-19h)
    ↓
Fetche l'emploi du temps depuis API ESEO
    ↓
Compare le hash avec l'ancien
    ↓
Si changement détecté:
    ↓
    Envoie notification via send_firebase_notification()
    ↓
    Firebase Cloud Messaging push vers l'appareil
```

### 4. Logout

```
User se déconnecte (DELETE /auth/logout)
    ↓
Backend supprime User.device_token
    ↓
Plus de notifications envoyées
```

---

## 🔄 Migration vers Firebase Admin SDK (Recommandé)

L'API FCM Legacy sera dépréciée. Voici comment migrer vers Firebase Admin SDK :

### Étape 1 : Installer Firebase Admin SDK

Ajouter dans `requirements.txt` :
```txt
firebase-admin>=6.0.0
```

### Étape 2 : Obtenir le fichier serviceAccountKey.json

1. Firebase Console → **⚙️ Paramètres du projet** → **Comptes de service**
2. Cliquer **"Générer une nouvelle clé privée"**
3. Télécharger le fichier JSON
4. Renommer en `serviceAccountKey.json`
5. Placer dans le dossier du projet (ou `/app/config/` dans Docker)

⚠️ **IMPORTANT** : Ajouter `serviceAccountKey.json` au `.gitignore` !

### Étape 3 : Modifier scheduler.py

Remplacer la fonction `send_firebase_notification()` :

```python
import firebase_admin
from firebase_admin import credentials, messaging
import os

# Initialiser Firebase Admin (une seule fois au démarrage)
def initialize_firebase():
    """Initialize Firebase Admin SDK"""
    if not firebase_admin._apps:
        cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "./serviceAccountKey.json")
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)

def send_firebase_notification(device_token: str, title: str, body: str, data: dict = None):
    """
    Send push notification via Firebase Admin SDK

    Args:
        device_token: FCM device token
        title: Notification title
        body: Notification body
        data: Additional data payload
    """
    try:
        # S'assurer que Firebase est initialisé
        initialize_firebase()

        # Construire le message
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            token=device_token,
            android=messaging.AndroidConfig(
                priority='high',
                notification=messaging.AndroidNotification(
                    sound='default',
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound='default',
                    ),
                ),
            ),
        )

        # Ajouter les données si présentes
        if data:
            message.data = data

        # Envoyer
        response = messaging.send(message)
        print(f"Notification sent successfully: {response}")
        return response

    except Exception as e:
        print(f"Error sending notification: {e}")
        return None
```

### Étape 4 : Configurer l'environnement

Dans `.env` :
```bash
# Chemin vers le fichier de credentials Firebase
FIREBASE_CREDENTIALS_PATH=/app/config/serviceAccountKey.json
```

### Étape 5 : Monter le fichier dans Docker

Dans `docker-compose.yml` :
```yaml
services:
  edt-backend:
    volumes:
      - edt-data:/app/data
      - ./serviceAccountKey.json:/app/config/serviceAccountKey.json:ro  # Ajouter ceci
```

### Étape 6 : Redéployer

```bash
./deploy.sh update
```

---

## 🆘 Dépannage

### Notification ne s'envoie pas

**Vérifier les logs** :
```bash
docker compose logs -f edt-backend | grep -i notification
```

**Causes courantes** :
1. ❌ `FCM_SERVER_KEY` non configurée
   - Solution : Ajouter dans `.env`

2. ❌ Token FCM invalide
   - Solution : Flutter doit re-enregistrer le token

3. ❌ API Legacy désactivée
   - Solution : Migrer vers Firebase Admin SDK

4. ❌ Firewall bloque FCM
   - Solution : Autoriser `fcm.googleapis.com`

### Flutter ne reçoit pas la notification

**Vérifier dans Flutter** :

```dart
// Écouter les notifications en foreground
FirebaseMessaging.onMessage.listen((RemoteMessage message) {
  print('Notification reçue: ${message.notification?.title}');
});

// Écouter les notifications en background
FirebaseMessaging.onBackgroundMessage(_firebaseMessagingBackgroundHandler);
```

**Permissions** :

Sur Android, vérifier dans `AndroidManifest.xml` :
```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED"/>
```

---

## 📊 Monitoring

### Vérifier les tokens enregistrés

```bash
# SSH sur le serveur
ssh user@serveur.com
cd /opt/edt-eseo

# Accéder au container
docker exec -it edt-backend python

# Dans le shell Python
from database import SessionLocal, User
db = SessionLocal()
users_with_tokens = db.query(User).filter(User.device_token.isnot(None)).all()
print(f"Utilisateurs avec notifications: {len(users_with_tokens)}")
for user in users_with_tokens:
    print(f"  - ESEO ID: {user.eseo_id}, Token: {user.device_token[:20]}...")
```

### Tester manuellement une notification

```python
from scheduler import send_firebase_notification

send_firebase_notification(
    device_token="TOKEN_FCM_DU_TELEPHONE",
    title="Test notification",
    body="Ceci est un test manuel"
)
```

---

## 📈 Statistiques Firebase

Pour voir les statistiques d'envoi :

1. Firebase Console → **Cloud Messaging**
2. Onglet **"Rapports"**
3. Voir :
   - Notifications envoyées
   - Taux de livraison
   - Erreurs

---

## 🔗 Liens utiles

- [Firebase Cloud Messaging](https://firebase.google.com/docs/cloud-messaging)
- [Firebase Admin SDK Python](https://firebase.google.com/docs/admin/setup)
- [Flutter Firebase Messaging](https://firebase.flutter.dev/docs/messaging/overview)
- [Migration API Legacy → v1](https://firebase.google.com/docs/cloud-messaging/migrate-v1)

---

## ✅ Checklist de configuration

- [ ] Projet Firebase créé
- [ ] Flutter app configurée avec Firebase
- [ ] FCM Server Key obtenue
- [ ] `FCM_SERVER_KEY` ajoutée dans `.env` du serveur
- [ ] Backend redémarré
- [ ] Flutter enregistre le token avec `POST /auth/register-device`
- [ ] Test d'envoi de notification réussi
- [ ] Notification reçue sur l'appareil
- [ ] (Optionnel) Migration vers Firebase Admin SDK
