# gtask-to-ourgroceries

Pont de synchronisation serverless (**Google Keep → OurGroceries**, compatible aussi avec **Google Tasks**) hébergé gratuitement sur **Vercel** et déclenché par **cron-job.org**.

Il permet de parler naturellement à **Gemini for Home / Google Home** (*« Hey Google, ajoute du lait à ma liste de courses »*) et de retrouver automatiquement ses articles dans **OurGroceries**, sans créer de doublons grâce à un **matching intelligent par IA (Gemini Flash)** — avec repli automatique sur un nettoyage syntaxique et Fuzzy Matching français — sur les articles existants (actifs ou barrés) de la liste cible.

---

## Architecture

```
[Gemini for Home / Google Home]
       │ (Vocal : "Ajoute du lait à ma liste de courses")
       ▼
[Google Keep] (Une ou plusieurs listes tampon, personnelles ou partagées)
       │
       ▼  (Toutes les 5 min via cron-job.org -> GET /api/sync)
[Vercel Serverless Function (Python)]
  1. Lit les articles non cochés des listes Google Keep configurées via gkeepapi
     (S'arrête immédiatement s'il n'y a aucun article : 0 appel Gemini à vide)
  2. Réconciliation intelligente via Gemini Flash (gratuit Google AI Studio)
     avec les éléments présents (actifs ou barrés) de la liste cible OurGroceries :
     • Associe marques/variantes habituelles ("Lait" -> "lait cawol") et synonymes
     • Fallback automatique sur le Fuzzy Matching local (mots-clés, accents, pluriels)
     • Match barré   -> Décoche l'article existant (conserve sa catégorie)
     • Match actif   -> Ignore (évite les doublons)
     • Aucun match   -> Crée le nouvel article (avec auto-catégorisation)
  3. Coche les articles synchronisés dans Google Keep
       │
       ▼
[Application OurGroceries]
```

*(Note : Si `GOOGLE_KEEP_MASTER_TOKEN` n'est pas défini mais que `GOOGLE_TASK_LIST_ID` est renseigné, le script bascule automatiquement sur Google Tasks en mode compatibilité.)*

---

## Structure du projet

```text
├── api/
│   └── sync.py              # Fonction Serverless Vercel (/api/sync)
├── scripts/
│   └── setup_wizard.py      # Assistant interactif local (Google Keep + OurGroceries + Gemini)
├── tests/
│   └── test_matching.py     # Tests unitaires du Fuzzy Matching français
├── .env.example             # Modèle des variables d'environnement
├── pyproject.toml           # Configuration Python / point d'entrée Vercel
├── requirements.txt         # Dépendances Python installées par Vercel
└── vercel.json              # Configuration Vercel
```

---

## Guide d'installation pas à pas

### Étape 1 : Lancer l'assistant local (`setup_wizard.py`)

Plus besoin de projet Google Cloud ni d'écran de consentement OAuth2 pour Google Keep !
Sur votre ordinateur (Python 3.11+), exécutez :

```bash
pip install -r requirements.txt gpsoauth
python scripts/setup_wizard.py
```

L'assistant va vous guider pas à pas pour :
1. **Google Keep (`GOOGLE_KEEP_EMAIL`, `GOOGLE_KEEP_MASTER_TOKEN`, `GOOGLE_KEEP_NOTE_ID`)** :
   - Ouvrir `https://accounts.google.com/EmbeddedSetup` dans votre navigateur.
   - Récupérer une seule fois le cookie `oauth_token` (`F12 > Application > Cookies > https://accounts.google.com > oauth_token`) et le convertir automatiquement en Master Token permanent (`aas_et/...`).
   - Afficher toutes vos listes Google Keep — **personnelles comme partagées avec votre compte** — (avec le nombre d'articles cochés / non cochés) et en sélectionner **une ou plusieurs** (ex. `1,2`).
2. **OurGroceries (`OG_USERNAME`, `OG_PASSWORD`, `OG_LIST_ID`)** :
   - Se connecter à votre compte et choisir la liste cible OurGroceries.
3. **Gemini AI (`GEMINI_API_KEY`)** *(recommandé, gratuit)* :
   - Renseigner et tester votre clé gratuite générée sur [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
4. **Sécurité (`CRON_SECRET`)** :
   - Générer (ou conserver) un jeton secret et afficher le récapitulatif des variables à copier dans Vercel.

---

### Étape 2 : Déployer sur Vercel

1. Poussez les fichiers sur votre dépôt GitHub :
   ```bash
   git add .
   git commit -m "feat: switch primary source to Google Keep"
   git push origin main
   ```
2. Connectez-vous sur [Vercel](https://vercel.com/) → **Settings > Environment Variables** de votre projet (ou lors d'un import **Add New... > Project**), et ajoutez les variables affichées par l'assistant :

| Variable | Description |
| :--- | :--- |
| `GOOGLE_KEEP_EMAIL` | Adresse e-mail du compte Google Keep |
| `GOOGLE_KEEP_MASTER_TOKEN` | Master Token permanent (`aas_et/...`) généré par `setup_wizard.py` |
| `GOOGLE_KEEP_NOTE_ID` | Un ou plusieurs IDs (ou titres exacts) de listes Google Keep séparés par des virgules (ex. `id1,id2`), y compris des listes partagées avec ce compte |
| `OG_USERNAME` | Adresse e-mail de votre compte OurGroceries |
| `OG_PASSWORD` | Mot de passe OurGroceries |
| `OG_LIST_ID` | ID de la liste de courses OurGroceries cible |
| `GEMINI_API_KEY` | *(Recommandé, gratuit)* Clé Google AI Studio pour le matching intelligent |
| `GEMINI_MODEL` | *(Optionnel)* Modèle Gemini utilisé (défaut : `gemini-3.5-flash-lite`) |
| `CRON_SECRET` | Jeton secret protégeant l'endpoint `/api/sync` |
| `MATCH_CUTOFF` | *(Optionnel)* Seuil de similarité de secours entre `0.0` et `1.0` (défaut : `0.75`) |

3. Déployez le projet sur Vercel.

---

### Étape 3 : Tester et automatiser avec cron-job.org

#### Test manuel dans le navigateur
Une fois déployé sur Vercel, ajoutez un article dans votre liste Google Keep (ou demandez à Google Home) puis testez en mode simulation `dry_run=1` :
```text
https://VOTRE-PROJET.vercel.app/api/sync?secret=VOTRE_CRON_SECRET&dry_run=1
```
Puis en conditions réelles (cochera l'article dans Keep et le poussera dans OurGroceries) :
```text
https://VOTRE-PROJET.vercel.app/api/sync?secret=VOTRE_CRON_SECRET
```

#### Automatisation toutes les 5 minutes sur cron-job.org
1. Créez un compte gratuit sur [cron-job.org](https://cron-job.org/).
2. Cliquez sur **Create Cronjob** :
   - **Title** : `Sync Google Keep to OurGroceries`
   - **URL** : `https://VOTRE-PROJET.vercel.app/api/sync`
   - **Execution schedule** : Every **5 minutes**
3. Dans l'onglet **Advanced** → **Headers**, ajoutez :
   - Key : `Authorization`
   - Value : `Bearer VOTRE_CRON_SECRET`
   *(Alternative : utilisez directement l'URL `https://VOTRE-PROJET.vercel.app/api/sync?secret=VOTRE_CRON_SECRET`)*
4. Cliquez sur **Create**.
