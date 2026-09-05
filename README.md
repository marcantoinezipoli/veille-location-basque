# Veille location Côte Basque

Petit outil qui visite chaque matin les pages « locations » d'une trentaine d'agences
d'Anglet, Biarritz, Bidart et Bayonne, repère les annonces apparues depuis la veille
et produit une page HTML consultable depuis le téléphone, nouveautés en tête.

Il ne lit **pas** Leboncoin, SeLoger, Bien'ici ni PAP (protections anti-robots,
conditions d'utilisation). Pour ces portails : alertes e-mail natives + Jinka.
Les deux ensemble couvrent l'essentiel du marché.

## Ce qu'il fait concrètement

1. Ouvre l'URL de chaque agence (`agences.json`). Si c'est une page d'accueil,
   cherche seul la rubrique « louer / location à l'année ».
2. Relève tous les liens qui ressemblent à une annonce (et écarte vente, saisonnier,
   parking, bureaux, pages de navigation).
3. Compare avec `etat.json` : tout lien jamais vu = nouveauté.
4. Lit sur la carte de l'annonce ce qu'il peut (loyer, surface, T3, chambres) et classe :
   **Correspond** / **À vérifier** (infos incomplètes) / **Hors critères**.
   Critères actuels : 2 chambres minimum, 2 200 € CC maximum, appartement ou maison, meublé ou vide,
   pas de rez-de-chaussée (ni rez-de-jardin), location à l'année uniquement (étudiant, bail mobilité,
   bail civil, saisonnier, colocation rejetés même si mentionnés seulement dans la description).
5. Ouvre la fiche complète de chaque nouveauté (40 par jour maximum) pour lire la description,
   le DPE, meublé/vide, et compléter surface / loyer / chambres si la liste ne les donnait pas.
6. Calcule un **score** : mots-clés positifs (rénové, lumineux, terrasse, calme, architecte, Cinq
   Cantons…) et négatifs (à rafraîchir, travaux, rez-de-chaussée, sans ascenseur, vis-à-vis, étudiant,
   bail mobilité, DPE F/G…), plus des bonus surface / loyer / DPE. Au-delà du seuil, l'annonce est
   marquée **coup de cœur ♥** et remonte en tête.
7. Écrit `docs/index.html`, trié par score. Les annonces non revues depuis 3 jours sont retirées.

Le premier passage enregistre tout comme base de référence sans rien surligner ;
les nouveautés apparaissent à partir du deuxième.

## Installation (10 minutes, option recommandée : GitHub Actions + GitHub Pages)

Résultat : le rapport se met à jour seul chaque matin, à une adresse du type
`https://<ton-pseudo>.github.io/veille-location-basque/`, sans ordinateur allumé.

1. Créer un compte GitHub si besoin, puis un **nouveau dépôt** nommé `veille-location-basque`
   (public ou privé ; pour un dépôt privé, GitHub Pages exige un compte Pro — sinon garder public,
   il n'y a rien de personnel dedans).
2. Y déposer tous les fichiers de ce dossier (bouton *Add file → Upload files*, glisser-déposer le
   contenu du zip, y compris le dossier caché `.github` — si l'upload web l'ignore, créer le fichier
   `.github/workflows/veille.yml` à la main via *Add file → Create new file* et coller son contenu).
3. Onglet **Settings → Actions → General** : dans *Workflow permissions*, cocher
   **Read and write permissions**, enregistrer.
4. Onglet **Settings → Pages** : *Source* = *Deploy from a branch*, branche `main`, dossier `/docs`,
   enregistrer. L'adresse publique s'affiche quelques minutes plus tard.
5. Onglet **Actions** → « Veille location quotidienne » → **Run workflow** : premier passage
   (crée la base). Relancer une fois le lendemain, ou attendre le cron du matin.

Ensuite, ouvrir l'adresse GitHub Pages chaque matin (l'ajouter à l'écran d'accueil du téléphone).

## Ce que contient le rapport (docs/index.html)

- Onglets **Nouveau / ♥ Coups de cœur / Tout / 🗺 Carte / Écartées**, filtre par ville.
- Chaque annonce : photo, loyer, T3 / chambres / m² / DPE, loyer au m² comparé à la médiane de la ville
  (« bon prix » ≤ −10 %, « au-dessus du marché » ≥ +15 %), temps estimé jusqu'au CH Côte Basque,
  mots-clés positifs et négatifs, puis boutons **Voir l'annonce**, **📞 appeler**, **✉️ e-mail pré-rédigé**
  (texte dans le bloc `contact` de `agences.json`), **📋 copier le message**, et suivi
  **Vu / Contacté / Visite / Non** mémorisé sur l'appareil.
- **Carte** : repères OpenStreetMap avec le loyer, placés sur le quartier cité dans l'annonce
  (table `lieux.quartiers`), sinon sur la ville. Position approximative par nature.
- **Baisses de loyer** : une annonce déjà vue dont le loyer baisse d'au moins 3 % remonte en « Nouveau »
  avec un badge bleu.
- **Historique de prix** : chaque changement de loyer est daté et affiché sur la carte
  (« 1 800 € (4 sept.) → 1 690 € (5 sept.) ») ; au-delà de 14 jours en ligne, l'annonce porte
  une étiquette « En ligne depuis N j », et « négociable ? » à partir de 21 jours.
- **Tendances 7 jours** en bas de page, et dans l'e-mail du lundi.
- **Trois passages par jour** (7 h 30, 13 h, 18 h 30 heure de Paris en été). Le digest du matin
  part toujours ; les deux autres n'envoient un e-mail que s'il y a du nouveau.

## Recevoir le rapport par e-mail chaque matin

Le script envoie un digest (nouveautés + meilleures annonces en ligne) via Gmail si trois secrets
sont définis dans le dépôt : *Settings → Secrets and variables → Actions → New repository secret*.

| Nom | Valeur |
|---|---|
| `MAIL_USER` | l'adresse Gmail qui envoie (ex. `prenom@gmail.com`) |
| `MAIL_PASSWORD` | un **mot de passe d'application** Google (pas le mot de passe du compte) |
| `MAIL_TO` | destinataires, séparés par des virgules |

Mot de passe d'application : myaccount.google.com → Sécurité → Validation en deux étapes (doit être
activée) → « Mots de passe des applications » → créer → copier les 16 caractères.
Sans ces secrets, le script tourne normalement et écrit simplement « E-mail non envoyé ».

## Installation alternative : sur ton ordinateur

```bash
pip install -r requirements.txt
python veille.py --test        # vérifie chaque agence, n'enregistre rien
python veille.py               # passage réel → docs/index.html
```

Puis programmer `python veille.py` chaque matin (Planificateur de tâches Windows,
ou `crontab -e` sur Mac/Linux : `30 7 * * * cd /chemin/veille-location-basque && python3 veille.py`).
Ouvrir `docs/index.html` dans le navigateur.

## Premier lancement : calibrer la liste

Lancer `python veille.py --test`. Pour chaque agence s'affiche le nombre de liens reconnus
et les cinq premiers. Trois cas :

- **ok, N annonces** avec des titres cohérents → rien à faire.
- **vide** → soit l'agence n'a aucune location en ligne ce jour-là (fréquent en septembre),
  soit son site est entièrement en JavaScript (Foncia, certaines franchises) et le script
  ne voit pas les annonces. Ouvrir l'URL à la main : si des annonces existent, chercher dans le menu
  une page « Nos locations » au format classique et remplacer `url` dans `agences.json`.
  Sinon, supprimer l'agence de la liste et s'appuyer sur ses alertes e-mail.
- **erreur** → site protégé ou URL erronée. Même démarche.

Les entrées marquées `"verifie": false` dans `agences.json` sont des URL déduites du schéma
du site (par exemple Foncia Bayonne sur le modèle de Foncia Anglet) : à confirmer en priorité.

Le tableau « État des agences surveillées » en bas du rapport reprend ces informations
à chaque passage, donc une agence qui casse se voit tout de suite.

## Ajouter une agence

Dans `agences.json`, ajouter une ligne :

```json
{"nom": "Nom de l'agence", "ville": "Biarritz", "type": "indépendante",
 "url": "https://www.exemple.fr/locations", "verifie": true}
```

Mettre de préférence l'URL de la page qui liste les locations à l'année ; à défaut, la page d'accueil.

## Ajuster le score (bloc `scoring` de `agences.json`)

- `mots_plus` / `mots_moins` : un mot → des points. Ajouter, retirer ou changer les valeurs librement
  (« rénové avec goût » : 3, « vue mer » : 2, « travaux » : -3…). La recherche ignore majuscules et
  accents typographiques, et neutralise « sans terrasse », « pas de parking ».
- `bonus_objectifs` : points selon surface (≥ 70, ≥ 80 m²), loyer (≤ 1 500, ≤ 1 700 €) et DPE.
- `coup_de_coeur_a_partir_de` : seuil du ♥ (5 par défaut).

Les points ne servent qu'au tri ; ils n'excluent jamais une annonce. Seuls les critères durs
(`criteres`) le font.

## Ajuster les critères

Bloc `criteres` dans `agences.json` : `min_chambres`, `min_pieces`, `min_surface`, `max_loyer`,
`mots_exclus` (cherchés dans le **titre** uniquement : studio, parking, T2…), `mots_eliminatoires` (cherchés **partout**, description comprise : rez-de-chaussée, étudiant, bail mobilité, saisonnier…), `villes_acceptees`, `dpe_minimum` (F et G exclus par défaut).
Une annonce dont la carte ne donne aucune caractéristique n'est jamais exclue : elle est « À vérifier ».

## Sources testées mais illisibles pour le script

Annonces chargées en JavaScript ou site protégé (vérifié le 4 septembre 2026) : Foncia, Laforêt,
Human Immobilier, Guy Hoquet, Cabinet de Lesseps, Adour Gestion, Superimmo. Pour celles-là, créer
une alerte e-mail sur leur site : c'est gratuit et immédiat.

## Agences repérées sans site exploitable

Quelques enseignes locales sont connues mais sans page de locations identifiable en ligne ;
elles publient surtout sur les portails ou à l'agence : Anglet Immo (FNAIM), MC Immo Biarritz,
Orpi Saint-Martin et Orpi Agence des Halles à Biarritz (agences Orpi : l'annuaire
orpi.com permet de retrouver leur page puis d'ajouter `/louer`), Barnes et Sotheby's Côte Basque
(location surtout haut de gamme et saisonnière). Un passage physique ou un e-mail avec le dossier
reste le meilleur canal pour ces agences-là.

## Limites à garder en tête

- Le script reconnaît des liens, pas des annonces au sens strict : il peut remonter quelques
  faux positifs (page de résultats d'une autre commune, bien déjà loué encore affiché). Le classement
  par critères et le titre permettent de trier en dix secondes.
- Les sites changent : une agence qui passe à « vide » d'un coup a probablement refait son site.
- Rythme volontairement doux (une requête par agence et par jour, délai entre sites) : c'est un
  usage équivalent à une consultation manuelle.
- Groupes Facebook : hors de portée de l'outil, à surveiller à la main.
