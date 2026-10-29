# Extraction MSCI ESG

Interface locale pour enrichir une liste d'ISIN avec l'API MSCI ESG et télécharger le fichier
d'import **au format exact de l'équipe** : un classeur avec les deux feuilles
`Template Import Titres` (94 colonnes) et `Template Import Fonds` (123 colonnes). Les en-têtes,
leur ordre et leurs styles sont repris du template de [RcoMehdi/MSCI-ESG](https://github.com/RcoMehdi/MSCI-ESG).

1. **Ajouter les ISIN** : déposer un Excel/CSV, ou coller une liste, côté Titres ou Fonds.
2. **Extraire** : l'application interroge MSCI et montre l'avancement.
3. **Télécharger l'Excel** : un aperçu montre, pour chaque ISIN et chaque colonne, si la cellule
   est remplie et par quelle source (survol = valeur, source, date).

## Démarrer

Prérequis : Python 3.11 ou plus récent. Aucune dépendance au-delà de `requests` et `openpyxl`
(les mêmes que le projet de Mehdi) ; pas de CDN JavaScript.

**Windows** : double-cliquer sur `lancer.bat`. La première fois, il crée `.venv` et installe
les dépendances ; le navigateur s'ouvre ensuite sur `http://127.0.0.1:8765/`.

**Terminal** (toutes plateformes) :

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows : .venv\Scripts\python.exe
.venv/bin/python -m app                                  # --demo pour des données fictives
```

Identifiants MSCI : copier `.env.example` en `.env` et renseigner `MSCI_CLIENT_ID` et
`MSCI_CLIENT_SECRET`, ou les saisir dans l'interface (bouton en haut à droite). Saisis dans
l'interface, ils restent uniquement en mémoire du processus local. Le serveur n'écoute que sur
`127.0.0.1` et ne renvoie jamais les identifiants au navigateur.

Le mode démonstration (`--demo`, ou le lien dans la fenêtre de connexion) permet de prendre
l'outil en main sans compte : valeurs fictives, bandeau visible, fichier préfixé `DEMO_`.

## Fichiers acceptés en entrée

- `.xlsx`, `.xlsm`, `.csv`. L'en-tête `ISIN` est cherché dans les 10 premières lignes de chaque
  feuille (`ISIN`, `Code ISIN`, `FUND_ISIN`, `ISSUER_ISIN`…). Sans en-tête, la colonne qui
  contient le plus d'ISIN est retenue.
- Les feuilles nommées *Titres* ou *Fonds* (format `data/input.xlsx` de Mehdi) vont dans le bon
  onglet ; les autres vont dans l'onglet ouvert.
- Colonnes facultatives `Client_ID` et `Security_Status`, recopiées dans l'export.
- Les formules sont lues par leur valeur calculée. L'ordre et les doublons sont conservés.
- En saisie manuelle : un ISIN par ligne, avec `Client_ID` et `Security_Status` après une
  tabulation (copier-coller direct depuis Excel).

Les ISIN sont contrôlés (format + clé de Luhn) pendant la frappe ; un ISIN invalide n'est
jamais envoyé à MSCI mais garde sa ligne dans l'export.

## Pourquoi l'extraction précédente ne remontait pas tout

Le `diagnostics.json` du dépôt de Mehdi (11 ISIN) montre que la requête V4 `/issuers` ne
renvoie que les références : **81 des 89 codes demandés** sont rejetés avec
*« not recognized or not available to this user »*, et 4 des 8 restants n'ont pas de valeur.
Trois causes, et ce que fait cette version :

| Cause | Ce que fait cette application |
|---|---|
| La V4 courante filtre par les nouveaux *current-day products* (doc p. 4). Un facteur visible au catalogue peut être refusé en données. | **Cascade de sources** : V4, puis V3 courante (droits ESG Manager) et enfin l'historique V4 (dernière valeur publiée), **uniquement pour les cellules encore vides**. Chaque valeur garde sa source et sa date. |
| Certains codes du mapping n'existent pas côté émetteur : `ESG_ENVIRONMENTAL_SCORE`, `ESG_SOCIAL_SCORE` et `ESG_GOVERNANCE_SCORE` sont des facteurs **Fonds** ; `ESG_RATING_DATE` n'existe pas au catalogue. | Chaque colonne a une liste ordonnée de codes candidats. Ajouts (vérifiés dans le catalogue MSCI) : `ENVIRONMENTAL_PILLAR_SCORE`, `SOCIAL_PILLAR_SCORE`, `GOVERNANCE_PILLAR_SCORE`, `IVA_RATING_DATE` (« ESG Rating Date »). Le code d'origine reste prioritaire. |
| Obligations et filiales sans notation propre (ex. `US02220AAA51`). | `inherit_missing_values` : valeurs de l'émetteur parent (désactivable ; retiré automatiquement si MSCI le refuse). |

Optimisations de transport :

- 100 identifiants et 100 facteurs par requête émetteur (limites p. 6), 50 pour les fonds ;
  la version précédente plafonnait à 50 émetteurs.
- Lots suivants (`data_request_id` / `batch_id`) et paquets d'identifiants téléchargés en
  parallèle (4 connexions par défaut, `MSCI_WORKERS`), ce que la doc autorise (p. 12).
- Un seul token OAuth par session, partagé et réutilisé jusqu'à expiration (p. 8).
- Fonds : l'ISIN est d'abord envoyé directement à `/funds` ; seuls les ISIN non reconnus passent
  par `/coverage` (ISIN → MDSUID), mis en cache pour la journée. Si `MSCI_FUND_PRODUCTS` est vide,
  les produits Fonds du compte sont lus via `/metadata/products`.
- La V4 n'est interrogée que pour les codes présents à son catalogue ; les autres sont tentés
  directement en V3 et en historique.
- Une source secondaire refusée (403) ou en erreur n'interrompt pas l'extraction : elle est
  signalée dans l'interface et dans le diagnostic.

Règles reprises à l'identique : aucune valeur imputée, un `0` ou `False` MSCI est une valeur,
`FUND_SHARE_CLASS_ID` reste vide tant que sa correspondance n'est pas validée,
`FUND_IMPLIED_TEMP_RISE` n'est jamais utilisé côté émetteur, texte forcé dans l'Excel.

## Ce qui reste à confirmer sur un vrai compte

Aucun appel au service MSCI réel n'a été fait pendant le développement (pas d'identifiants).
Le code suit la documentation et les structures de réponse observées par Mehdi ; à vérifier au
premier lancement :

- que le compte a des droits V3 ou historiques sur les facteurs refusés en V4. Sinon le
  diagnostic l'indique colonne par colonne (« Non autorisé par l'abonnement ») et seule MSCI peut
  ouvrir ces données ;
- que la V3 accepte le même token (audience `https://esg/data`), comme l'indique la doc ;
- la pertinence métier des valeurs historiques (date visible au survol et dans le diagnostic),
  et des quatre codes candidats ajoutés.

## Diagnostic

Le bouton **Diagnostic** télécharge un JSON : sources interrogées et leur statut, codes refusés
par source, codes absents du catalogue V4, avertissements et `trace_id` de chaque appel (utile
pour le support MSCI). Il contient des ISIN de portefeuille : ne pas le diffuser. Il ne contient
jamais de secret ni de token.

## Organisation du code

| Fichier | Rôle |
|---|---|
| `app/client.py` | OAuth, HTTP, reprises bornées (429/5xx, `Retry-After`, 5 min sur `/coverage` 503), lots parallèles |
| `app/engine.py` | Cascade V4 → V3 → historique, résolution fonds, états de ligne |
| `app/excel_io.py` | Lecture souple des ISIN, export dans une copie du template |
| `app/mapping.py`, `app/data/mapping.json` | Une règle par colonne, dans l'ordre du template, avec ses codes candidats |
| `app/data/catalog.json` | Libellés et descriptions MSCI des codes utilisés (extrait du catalogue fourni) |
| `app/data/template.xlsx` | Les deux feuilles template, copiées sans modification du `mapping.xlsx` de l'équipe |
| `app/server.py`, `app/jobs.py` | Serveur local (bibliothèque standard), extractions en arrière-plan |
| `app/static/` | Interface (HTML/CSS/JS sans dépendance) |
| `app/mock.py` | Client simulé du mode démonstration |

Pour modifier une correspondance, éditer `app/data/mapping.json` (l'application refuse de
démarrer si les colonnes ne correspondent plus exactement au template).

Tests hors connexion : `pip install pytest` puis `python -m pytest`.

## Documentation de référence

Fichiers à la racine : *ESG Data API V4 Documentation (juillet 2026)*, *ESG Report API V2*,
*FAQ Instrument Reference API* et le catalogue *MSCI ESG & Climate Historical Data API*.
