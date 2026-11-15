# gtask-to-ourgroceries

Pont de synchronisation serverless (**Google Tasks → OurGroceries**) hébergé gratuitement sur **Vercel** et déclenché par **cron-job.org**.

Il permet de parler naturellement à **Gemini for Home** (*« Hey Google, ajoute du lait à ma liste de courses »*) et de retrouver automatiquement ses articles dans **OurGroceries**, sans créer de doublons grâce à un nettoyage syntaxique français et un **Fuzzy Matching** sur les articles existants (actifs ou barrés) de la liste cible.

---

## Architecture

```
[Gemini for Home]
       │ (Vocal : "Ajoute des tomates à ma liste de courses")
       ▼
[Google Tasks] (Liste tampon)
       │
       ▼  (Toutes les 5 min via cron-job.org -> GET /api/sync)
[Vercel Serverless Function (Python)]
  1. Lit les tâches non cochées via l'API Google Tasks (OAuth2 Refresh Token)
  2. Nettoie le texte ("ajoute des tomates" -> "tomates")
  3. Fuzzy Matching (accents, ligatures œ/oe, singulier/pluriel, Levenshtein >= 75%)
     avec les éléments présents (actifs ou barrés) de la liste cible OurGroceries :
     • Match barré   -> Décoche l'article existant (conserve sa catégorie)
     • Match actif   -> Ignore (évite les doublons)
     • Aucun match   -> Crée le nouvel article (avec auto-catégorisation)
  4. Marque la tâche comme "Terminée" dans Google Tasks
       │
       ▼
[Application OurGroceries]
```

---

## Structure du projet

```text
├── api/
│   └── sync.py              # Fonction Serverless Vercel (/api/sync)
├── scripts/
│   └── setup_wizard.py      # Assistant interactif local (OAuth2 Google + IDs des listes)
├── tests/
│   └── test_matching.py     # Tests unitaires du Fuzzy Matching français
├── .env.example             # Modèle des variables d'environnement
├── requirements.txt         # Dépendances Python installées par Vercel
└── vercel.json              # Configuration Vercel
```

---

## Guide d'installation pas à pas (en partant de zéro)

### Étape 1 : Créer les identifiants OAuth2 sur Google Cloud Console

> **Pourquoi OAuth2 et non un Service Account ?**
> Contrairement à Google Drive ou Sheets, l'API Google Tasks d'un compte personnel `@gmail.com` ne permet pas de partager une liste avec un *Service Account*. On utilise donc un `GOOGLE_REFRESH_TOKEN` OAuth2 généré une seule fois.

1. Rendez-vous sur la [Google Cloud Console](https://console.cloud.google.com/).
2. Créez un nouveau projet (ex: `gtask-ourgroceries`).
3. Activez l'API **Google Tasks API** :
   - Menu **APIs & Services > Library** → cherchez **Tasks API** → cliquez sur **Enable**.
4. Configurez l'écran de consentement (**APIs & Services > OAuth consent screen**) :
   - Choisissez **External** → remplissez le nom de l'application (ex: `gtask-sync`) et votre adresse e-mail.
   - Dans l'écran **Audience / Publishing status**, cliquez sur **Publish App** (*Publier l'application* / passer de *Testing* à *In production*).
   - ⚠️ **Important** : Sans cette étape *Publish App*, Google révoque les Refresh Tokens au bout de 7 jours. Vous n'avez pas besoin de soumettre l'application à validation Google (cliquez simplement sur *Paramètres avancés > Continuer vers gtask-sync* lors de la connexion).
5. Créez les identifiants OAuth2 (**APIs & Services > Credentials**) :
   - Cliquez sur **+ Create Credentials > OAuth client ID**.
   - Type d'application : **Desktop app** (*Application de bureau*).
   - Téléchargez le fichier JSON et enregistrez-le sous le nom `credentials.json` à la racine de ce projet (il est ignoré par Git).

---

### Étape 2 : Générer vos clés avec l'assistant local (`setup_wizard.py`)

Sur votre ordinateur, installez Python (3.10+) puis exécutez :

```bash
pip install -r requirements.txt google-auth-oauthlib
python scripts/setup_wizard.py
```

L'assistant va :
1. Ouvrir votre navigateur pour autoriser l'accès à Google Tasks et récupérer votre `GOOGLE_REFRESH_TOKEN`.
2. Afficher toutes vos listes Google Tasks pour vous laisser choisir celle utilisée par Gemini for Home (`GOOGLE_TASK_LIST_ID`).
3. Se connecter à votre compte OurGroceries et afficher vos listes pour récupérer l'ID de la liste cible (`OG_LIST_ID`).
4. Générer un `CRON_SECRET` aléatoire et afficher le bloc récapitulatif prêt à copier dans Vercel.

---

### Étape 3 : Déployer sur Vercel

1. Poussez ce dépôt sur votre compte GitHub :
   ```bash
   git add .
   git commit -m "Initial commit: Google Tasks to OurGroceries bridge"
   git push origin main
   ```
2. Connectez-vous sur [Vercel](https://vercel.com/) → **Add New... > Project** → importez votre dépôt GitHub.
3. Dans la section **Environment Variables**, ajoutez les variables affichées par l'assistant :

| Variable | Description |
| :--- | :--- |
| `GOOGLE_CLIENT_ID` | Client ID OAuth2 Google Cloud |
| `GOOGLE_CLIENT_SECRET` | Client Secret OAuth2 Google Cloud |
| `GOOGLE_REFRESH_TOKEN` | Refresh Token généré par `setup_wizard.py` |
| `GOOGLE_TASK_LIST_ID` | ID de la liste Google Tasks à surveiller |
| `OG_USERNAME` | Adresse e-mail de votre compte OurGroceries |
| `OG_PASSWORD` | Mot de passe OurGroceries |
| `OG_LIST_ID` | ID de la liste de courses OurGroceries cible |
| `CRON_SECRET` | Jeton secret protégeant l'endpoint `/api/sync` |
| `MATCH_CUTOFF` | *(Optionnel)* Seuil de similarité entre `0.0` et `1.0` (défaut : `0.75`) |

4. Cliquez sur **Deploy**.

---

### Étape 4 : Tester et automatiser avec cron-job.org

#### Test manuel dans le navigateur
Une fois déployé sur Vercel, vous pouvez tester sans modifier vos listes grâce au mode `dry_run=1` :
```text
https://VOTRE-PROJET.vercel.app/api/sync?secret=VOTRE_CRON_SECRET&dry_run=1
```
Puis en conditions réelles :
```text
https://VOTRE-PROJET.vercel.app/api/sync?secret=VOTRE_CRON_SECRET
```

#### Automatisation toutes les 5 minutes sur cron-job.org
1. Créez un compte gratuit sur [cron-job.org](https://cron-job.org/).
2. Cliquez sur **Create Cronjob** :
   - **Title** : `Sync Google Tasks to OurGroceries`
   - **URL** : `https://VOTRE-PROJET.vercel.app/api/sync`
   - **Execution schedule** : Every **5 minutes**
3. Dans l'onglet **Advanced** → **Headers**, ajoutez :
   - Key : `Authorization`
   - Value : `Bearer VOTRE_CRON_SECRET`
   *(Alternative : utilisez directement l'URL `https://VOTRE-PROJET.vercel.app/api/sync?secret=VOTRE_CRON_SECRET`)*
4. Cliquez sur **Create**.
