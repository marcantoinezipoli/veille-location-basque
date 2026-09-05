#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille location Côte Basque
===========================
Visite chaque jour les pages "locations" d'une liste d'agences locales,
détecte les annonces nouvelles par rapport à la veille (approche par différence
de liens : tout lien d'annonce jamais vu = nouveauté), puis génère un rapport
HTML consultable (docs/index.html) avec les nouveautés en tête.

Usage :
    python veille.py               lancement normal (met à jour etat.json + docs/index.html)
    python veille.py --test        vérifie chaque agence, affiche le nombre de liens trouvés,
                                   n'écrit rien (utile au premier lancement)
    python veille.py --agence olai teste uniquement les agences dont le nom contient "olai"
    python veille.py --sortie chemin.html   change l'emplacement du rapport

Dépendances : requests, beautifulsoup4, lxml   (pip install -r requirements.txt)
"""

import argparse
import json
import re
import sys
import time
import html as htmlmod
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

ICI = Path(__file__).resolve().parent
FICHIER_CONFIG = ICI / "agences.json"
FICHIER_ETAT = ICI / "etat.json"
SORTIE_DEFAUT = ICI / "docs" / "index.html"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DELAI_ENTRE_SITES = 1.5          # politesse : secondes entre deux agences
JOURS_AVANT_DISPARU = 3          # non revu depuis N jours -> "disparu"
JOURS_CONSERVATION_DISPARU = 30  # purge de l'historique
MAX_FICHES_PAR_PASSAGE = 40      # fiches détaillées ouvertes au maximum par jour (politesse)
DELAI_ENTRE_FICHES = 1.0

# --- Heuristiques de reconnaissance des liens d'annonce ------------------------

# Un lien est candidat s'il contient un de ces motifs...
MOTIFS_INCLUS = re.compile(
    r"(location|louer|appartement|appart|maison|villa|bien|annonce|propert|"
    r"logement|detail|fiche|offre|/fr/|-t[1-6]-|\d{3,})", re.I)

# ...et aucun de ceux-ci (navigation, réseaux sociaux, vente, saisonnier, etc.)
MOTIFS_EXCLUS = re.compile(
    r"(vente|vendre|achat|acheter|acquerir|acquisition|investir|neuf/|programme|"
    r"saisonn|vacances|holiday|estimation|estimer|contact|mentions|legal|cookies|"
    r"confidential|rgpd|actualit|blog|article|honoraire|tarif|recrut|carriere|"
    r"login|connexion|compte|inscription|favori|alerte|newsletter|"
    r"facebook|instagram|linkedin|twitter|youtube|tiktok|pinterest|"
    r"mailto:|tel:|javascript:|\.pdf$|\.jpe?g$|\.png$|\.webp$|"
    r"page=\d|/page/\d|tri=|sort=|order=|ordre=|"
    r"parking|garage|box-|bureau|local-|commerc|terrain|entrepot|"
    r"syndic|gestion-locative|gerance|copropri|assurance|financement|pret|"
    r"qui-sommes|equipe|agence-immobiliere|agences-immobilieres|nos-agences|plan-du-site|sitemap|"
    r"prix-immobilier|prix-du-m|prix-m2|barometre|vendu|/actus|/actualites|/content/|content_only|"
    r"type-bien|/pratique/|/guide|/conseil|/dossier|/faq|/avis|/temoignage|catalog/|/ville/|/villes/|/carte|"
    r"/louer/?$|/location/?$|/locations/?$|/biens-louer/?\d?$|/biens-en-location/?$)", re.I)

# Liens vers une collection ("toutes nos annonces") : jamais une annonce, même avec un identifiant
TEXTES_COLLECTION = re.compile(r"^(toutes?\b|tous\b|voir tou|découvrir tou|decouvrir tou|nos biens|nos annonces|toutes nos)", re.I)

# Textes de liens qui désignent une rubrique, jamais une annonce
TEXTES_NAVIGATION = re.compile(
    r"^(voir|découvrir|decouvrir|toutes?|tous|nos biens|habitations?|immo pro|trouver|mon compte|"
    r"vendu|louer|location|locations|acheter|vendre|accueil|contact|en savoir|lire|suivant|précédent|"
    r"precedent|page|appartements?|maisons?|villas?|terrains?|bureaux?|locaux)\b.{0,45}$", re.I)

PARAMS_A_RETIRER = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                    "utm_term", "fbclid", "gclid", "ref", "origin"}

RE_PRIX = re.compile(r"(\d{1,2}[\s\u202f\u00a0.]?\d{3}|\d{3,4})\s*(?:€|euros?)", re.I)
RE_SURFACE = re.compile(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:m²|m2|m\s?²)", re.I)
RE_CHAMBRES = re.compile(r"(\d)\s*ch(?:ambre)?s?\b", re.I)
RE_PIECES = re.compile(r"\bT\s?(\d)\b|(\d)\s*pi[èe]ces?", re.I)
RE_TYPE = re.compile(r"\b(appartement|appart|maison|villa|duplex|loft)\b", re.I)
RE_DPE = re.compile(r"(?:\bdpe\b|classe\s+(?:[ée]nerg(?:ie|[ée]tique))|consommation\s+[ée]nerg[ée]tique)\s*[:\-–]?\s*([a-g])\b", re.I)
RE_MEUBLE = re.compile(r"\bmeubl[ée]e?s?\b", re.I)


# --- Utilitaires ---------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def charger_json(chemin, defaut):
    if chemin.exists():
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    return defaut


def sauver_json(chemin, donnees):
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=1, sort_keys=True)


def normaliser_url(url):
    """Supprime fragment et paramètres de tracking pour dédoublonner."""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k not in PARAMS_A_RETIRER]
    chemin = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), chemin, "", urlencode(q), ""))


def recuperer(url, essais=2):
    """Télécharge une page. Retourne (html, erreur)."""
    derniere_erreur = None
    for i in range(essais):
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                            "Accept-Language": "fr-FR,fr;q=0.9"},
                             timeout=25, allow_redirects=True)
            if r.status_code == 200 and r.content:
                if "charset" not in r.headers.get("Content-Type", "").lower():
                    r.encoding = r.apparent_encoding or "utf-8"
                return r.text, None
            derniere_erreur = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            derniere_erreur = type(e).__name__
        time.sleep(2)
    return None, derniere_erreur


def texte_compact(s):
    return re.sub(r"\s+", " ", s or "").strip()


def entier(x):
    try:
        return int(str(x).replace(" ", "").replace("\u202f", "").replace("\u00a0", "")
                   .replace(".", "").replace(",", "."))
    except ValueError:
        return None


# --- Découverte automatique de la page "locations" ------------------------------

def trouver_page_locations(html, url_base):
    """Sur une page d'accueil, cherche un lien vers la rubrique locations à l'année."""
    soup = BeautifulSoup(html, "lxml")
    hote = urlparse(url_base).netloc.lower()
    candidats = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url_base, a["href"])
        if urlparse(href).netloc.lower() != hote:
            continue
        txt = texte_compact(a.get_text()).lower()
        cible = (href + " " + txt).lower()
        if re.search(r"saisonn|vacances|holiday|vendre|vente|estim", cible):
            continue
        score = 0
        if re.search(r"louer|location", href.lower()):
            score += 2
        if re.search(r"\blouer\b|\blocation", txt):
            score += 2
        if re.search(r"ann[ée]e|longue|nos biens|biens", cible):
            score += 1
        if score >= 2:
            candidats.append((score, href))
    if not candidats:
        return None
    candidats.sort(key=lambda c: (-c[0], len(c[1])))
    return candidats[0][1]


# --- Extraction des annonces ----------------------------------------------------

def contexte_du_lien(a):
    """Texte autour du lien : le lien lui-même + son conteneur (carte d'annonce)."""
    txt = texte_compact(a.get_text())
    for tag in ("article", "li", "div"):
        parent = a.find_parent(tag)
        if parent is not None:
            t = texte_compact(parent.get_text(" "))
            if 10 <= len(t) <= 600:
                return txt, t
    return txt, txt


def extraire_champs(contexte):
    c = contexte
    prix = None
    for m in RE_PRIX.finditer(c):
        v = entier(m.group(1))
        if v and 250 <= v <= 6000:       # borne un loyer mensuel plausible
            prix = v
            break
    surface = None
    m = RE_SURFACE.search(c)
    if m:
        try:
            surface = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    chambres = None
    m = RE_CHAMBRES.search(c)
    if m:
        chambres = int(m.group(1))
    pieces = None
    m = RE_PIECES.search(c)
    if m:
        pieces = int(m.group(1) or m.group(2))
    typ = None
    m = RE_TYPE.search(c)
    if m:
        typ = m.group(1).lower().replace("appart", "appartement").replace("appartementement", "appartement")
    return prix, surface, chambres, pieces, typ


def extraire_annonces(html, url_page, agence):
    """Retourne {url_normalisee: annonce} pour tous les liens ressemblant à une annonce."""
    soup = BeautifulSoup(html, "lxml")
    hote = urlparse(url_page).netloc.lower()
    url_page_n = normaliser_url(url_page)
    trouvees = {}

    for a in soup.find_all("a", href=True):
        brut = a["href"].strip()
        if not brut or brut.startswith("#"):
            continue
        href = urljoin(url_page, brut)
        p = urlparse(href)
        if p.scheme not in ("http", "https"):
            continue
        if p.netloc.lower() != hote:
            continue
        if MOTIFS_EXCLUS.search(href):
            continue
        url_n = normaliser_url(href)
        if url_n == url_page_n:
            continue
        # profondeur : une annonce n'est jamais la racine ou un chemin trop court
        if len(p.path.strip("/")) < 4:
            continue
        if not MOTIFS_INCLUS.search(p.path + "?" + p.query):
            continue

        txt, ctx = contexte_du_lien(a)
        # preuve positive d'annonce : identifiant numérique dans l'URL, ou prix / surface dans la carte
        a_identifiant = bool(re.search(r"\d{4,}", p.path + "?" + p.query))
        a_chiffres = bool(re.search(r"€|m²|m2\b", ctx)) and len(ctx) >= 15
        if not (a_identifiant or a_chiffres):
            continue
        if TEXTES_COLLECTION.match(txt):
            continue
        if TEXTES_NAVIGATION.match(txt) and not a_identifiant:
            continue
        # exclut les mentions saisonnières / vente dans le texte de la carte
        if re.search(r"saisonni|vacances|\bvente\b|à vendre|a vendre|viager", ctx, re.I):
            continue
        # ignore les liens sans aucun contenu (icônes, boutons vides) sauf si l'URL est très "annonce"
        if len(ctx) < 10 and not re.search(r"\d{4,}", p.path):
            continue

        prix, surface, chambres, pieces, typ = extraire_champs(ctx)
        titre = txt if 8 <= len(txt) <= 140 else (ctx[:140] if ctx else url_n)
        if TEXTES_NAVIGATION.match(txt) and len(ctx) > len(txt) + 10:
            titre = ctx[:140]

        if url_n in trouvees:
            # garde la version la plus riche
            anc = trouvees[url_n]
            if 8 <= len(txt) <= 140 and not TEXTES_NAVIGATION.match(txt) and anc["titre"] == anc.get("contexte", "")[:140]:
                anc["titre"] = txt
            if len(ctx) > len(anc.get("contexte", "")):
                anc.update(titre=titre, contexte=ctx[:300], prix=prix or anc.get("prix"),
                           surface=surface or anc.get("surface"),
                           chambres=chambres or anc.get("chambres"),
                           pieces=pieces or anc.get("pieces"), type=typ or anc.get("type"))
            continue

        trouvees[url_n] = {
            "url": href, "agence": agence["nom"], "ville_agence": agence["ville"],
            "titre": titre, "contexte": ctx[:300],
            "prix": prix, "surface": surface, "chambres": chambres, "pieces": pieces, "type": typ,
        }
    return trouvees


# --- Classement par rapport aux critères ---------------------------------------

def classer(annonce, criteres):
    """Retourne 'match', 'a_verifier' ou 'exclu'."""
    ctx = (annonce.get("contexte", "") + " " + annonce.get("titre", "") + " " + annonce.get("description", "")).lower()
    # les mots exclus ne sont cherchés que dans le titre (et le résumé de la liste), pas dans la
    # description complète : "parking" dans une description est un atout, pas un parking à louer
    zone_exclusion = (annonce.get("titre", "") + " " + annonce.get("contexte", "")[:120]).lower()
    for mot in criteres.get("mots_exclus", []):
        if mot.lower() in zone_exclusion:
            return "exclu"
    # éliminatoires : cherchés dans tout le texte disponible (description comprise)
    ctx_norm = ctx.replace("’", "'")
    for mot in criteres.get("mots_eliminatoires", []):
        if mot.lower() in ctx_norm:
            annonce["motif_exclusion"] = mot
            return "exclu"
    types = criteres.get("types_acceptes")
    if types and annonce.get("type") and annonce["type"] not in types:
        return "exclu"
    villes = [v.lower() for v in criteres.get("villes_acceptees", [])]
    if villes:
        # zone géographique : on juge d'abord sur titre + URL + résumé de liste (la description
        # complète cite souvent toutes les villes du secteur en pied de page)
        chemin_url = urlparse(annonce.get("url", "")).path
        zone1 = (annonce.get("titre", "") + " " + chemin_url + " " + annonce.get("contexte", "")).lower()
        zone1 = zone1.replace("-", " ").replace("_", " ")
        zone2 = (annonce.get("description", "") or "").lower().replace("-", " ")
        codes_ok = set(str(c) for c in criteres.get("codes_postaux", []))
        exclues = [c.lower().replace("-", " ") for c in criteres.get("communes_exclues", [])]

        def verdict(z):
            if any(v in z for v in villes):
                return "ok"
            codes = set(re.findall(r"\b(\d{5})\b", z))
            if codes and codes_ok and not (codes & codes_ok):
                return "hors zone (" + sorted(codes)[0] + ")"
            for c in exclues:
                if re.search(r"(?<![a-zà-ÿ])" + re.escape(c) + r"(?![a-zà-ÿ])", z):
                    return "hors zone (" + c + ")"
            return None

        v1 = verdict(zone1)
        if v1 is None:
            v1 = verdict(zone2)
        if v1 and v1 != "ok":
            annonce["motif_exclusion"] = v1
            return "exclu"
    ok = True
    connu = False
    p, s, ch, pi = annonce.get("prix"), annonce.get("surface"), annonce.get("chambres"), annonce.get("pieces")
    if p is not None:
        connu = True
        ok &= p <= criteres.get("max_loyer", 10**9)
    if s is not None:
        connu = True
        ok &= s >= criteres.get("min_surface", 0)
    if ch is not None:
        connu = True
        ok &= ch >= criteres.get("min_chambres", 0)
    elif pi is not None:
        connu = True
        ok &= pi >= criteres.get("min_pieces", 0)
    if annonce.get("dpe") in ("F", "G") and criteres.get("dpe_minimum", "E") in "ABCDE":
        ok = False
    if not ok:
        return "exclu"
    return "match" if connu else "a_verifier"



# --- Lecture de la fiche détaillée et score --------------------------------------

def lire_fiche(url):
    """Ouvre la page de l'annonce et renvoie un texte descriptif (ou None)."""
    html, err = recuperer(url, essais=1)
    if not html:
        return {"texte": None, "h1": None, "photo": None, "tels": [], "mails": []}
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "nav", "noscript", "svg", "form"]):
        t.decompose()
    morceaux = []
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        morceaux.append(texte_compact(meta["content"]))
    h1 = soup.find("h1")
    if h1:
        morceaux.append(texte_compact(h1.get_text(" ")))
    corps = soup.find("main") or soup.find("article") or soup.body
    if corps:
        morceaux.append(texte_compact(corps.get_text(" "))[:5000])
    texte = " ".join(m for m in morceaux if m)
    titre_h1 = texte_compact(h1.get_text(" ")) if h1 else None
    photo = None
    og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
    if og and og.get("content", "").startswith("http"):
        photo = og["content"]
    if not photo:
        lien_img = soup.find("link", attrs={"rel": "image_src"})
        if lien_img and lien_img.get("href", "").startswith("http"):
            photo = lien_img["href"]
    if not photo:
        for img in soup.find_all("img"):
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("data-original") or img.get("src") or ""
            if not src and img.get("srcset"):
                src = img["srcset"].split(",")[0].split()[0]
            if not src:
                continue
            src = urljoin(url, src)
            if re.search(r"logo|icon|sprite|pixel|avatar|placeholder|blank|loader|\.svg|\.gif|base64", src, re.I):
                continue
            if re.search(r"\.(jpe?g|png|webp)(\?|$)|/photos?/|/images?/|/media/|/uploads?/", src, re.I):
                largeur = img.get("width")
                try:
                    if largeur and int(str(largeur).replace("px", "")) < 120:
                        continue
                except ValueError:
                    pass
                photo = src
                break
    tels, mails = [], []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.lower().startswith("tel:"):
            t = re.sub(r"[^\d+]", "", h[4:])
            if len(t) >= 9 and t not in tels:
                tels.append(t)
        elif h.lower().startswith("mailto:"):
            m = h[7:].split("?")[0].strip()
            if "@" in m and m not in mails and not re.search(r"noreply|no-reply|example", m, re.I):
                mails.append(m)
    if not tels:
        for m in re.finditer(r"(?<!\d)(0[1-9](?:[\s.\-]?\d{2}){4})(?!\d)", texte or ""):
            t = re.sub(r"\D", "", m.group(1))
            if t not in tels:
                tels.append(t)
            if len(tels) >= 2:
                break
    if not mails:
        for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.]+", texte or ""):
            if not re.search(r"noreply|no-reply|example|sentry|wixpress", m.group(0), re.I) and m.group(0) not in mails:
                mails.append(m.group(0))
            if len(mails) >= 2:
                break
    return {"texte": texte or None, "h1": titre_h1, "photo": photo, "tels": tels, "mails": mails}


def extraire_dpe(texte):
    m = RE_DPE.search(texte or "")
    return m.group(1).upper() if m else None


def scorer(annonce, scoring):
    """Calcule score, atouts et réserves à partir des mots-clés et des chiffres lus."""
    if not scoring:
        return 0, [], []
    texte = " ".join(str(annonce.get(k) or "") for k in ("titre", "contexte", "description")).lower()
    texte = texte.replace("’", "'")
    score, atouts, reserves = 0, [], []
    def present_sans_negation(mot):
        i = texte.find(mot.lower())
        while i != -1:
            avant = texte[max(0, i - 8):i]
            if not re.search(r"\b(sans|pas de|aucun[e]?|ni)\s*$", avant):
                return True
            i = texte.find(mot.lower(), i + 1)
        return False

    for mot, pts in scoring.get("mots_plus", {}).items():
        if present_sans_negation(mot):
            score += pts
            atouts.append(mot)
    for mot, pts in scoring.get("mots_moins", {}).items():
        if mot.lower() in texte:
            score += pts
            reserves.append(mot)
    b = scoring.get("bonus_objectifs", {})
    s, p, d = annonce.get("surface"), annonce.get("prix"), annonce.get("dpe")
    if s:
        if s >= 80:
            score += b.get("surface_80_plus", 0); atouts.append(f"{int(s)} m²")
        elif s >= 70:
            score += b.get("surface_70_plus", 0); atouts.append(f"{int(s)} m²")
    if p:
        if p <= 1700:
            score += b.get("loyer_1700_moins", 0); atouts.append(f"loyer {p} €")
        elif p <= 1900:
            score += b.get("loyer_1900_moins", 0); atouts.append(f"loyer {p} €")
    if d:
        if d in "ABC":
            score += b.get("dpe_a_b_c", 0); atouts.append(f"DPE {d}")
        elif d == "D":
            score += b.get("dpe_d", 0)
        elif d == "E":
            score += b.get("dpe_e", 0); reserves.append("DPE E")
        else:
            reserves.append(f"DPE {d}")
    # dédoublonne en gardant l'ordre (ex. "rénové" et "rénovation")
    def uniq(l):
        out = []
        for x in l:
            xl = x.lower()
            if any(xl in y.lower() or y.lower() in xl for y in out):
                continue
            out.append(x)
        return out
    return score, uniq(atouts), uniq(reserves)


def enrichir_par_fiche(annonce, criteres, scoring):
    """Lit la fiche, complète les champs manquants, recalcule classement et score."""
    fiche = lire_fiche(annonce["url"])
    texte, titre_h1, photo = fiche["texte"], fiche["h1"], fiche["photo"]
    annonce["fiche_lue"] = bool(texte)
    if photo:
        annonce["photo"] = photo
    if fiche["tels"]:
        annonce["tel"] = fiche["tels"][0]
    if fiche["mails"]:
        annonce["mail"] = fiche["mails"][0]
    if texte:
        annonce["description"] = texte[:1200]
        titre_actuel = annonce.get("titre", "")
        if titre_h1 and 8 <= len(titre_h1) <= 120 and (
                TEXTES_NAVIGATION.match(titre_actuel) or titre_actuel == annonce.get("contexte", "")[:140]):
            annonce["titre"] = titre_h1
        prix, surface, chambres, pieces, typ = extraire_champs(texte)
        for k, v in (("prix", prix), ("surface", surface), ("chambres", chambres), ("pieces", pieces), ("type", typ)):
            if annonce.get(k) is None and v is not None:
                annonce[k] = v
        annonce["dpe"] = extraire_dpe(texte) or annonce.get("dpe")
        annonce["meuble"] = bool(RE_MEUBLE.search(texte))
        annonce["classement"] = classer(annonce, criteres)
    annonce["score"], annonce["atouts"], annonce["reserves"] = scorer(annonce, scoring)



# --- Carte, distances, prix au m² ---------------------------------------------------

import math


def geolocaliser(annonce, lieux):
    """Attribue lat/lon : quartier cité dans le texte, sinon ville de l'agence."""
    if not lieux:
        return
    def nettoie(t):
        t = t.lower().replace("’", "'")
        return re.sub(r"s(?:ain)?t[ -]jean[ -]de[ -]luz", "sjdl", t)   # évite que "saint-jean" attrape Saint-Jean-de-Luz
    zone_forte = nettoie(" ".join(str(annonce.get(k) or "") for k in ("titre", "contexte")))
    zone_faible = nettoie((annonce.get("description") or "")[:600])
    quartiers = sorted((lieux.get("quartiers") or {}).items(), key=lambda kv: -len(kv[0]))  # noms longs d'abord
    for zone in (zone_forte, zone_faible):
        for nom, (lat, lon) in quartiers:
            if nom in zone:
                annonce["lat"], annonce["lon"], annonce["position"] = lat, lon, f"quartier {nom.title()}"
                return
    texte = zone_forte + " " + zone_faible
    ville = annonce.get("ville_agence")
    for v in (lieux.get("villes") or {}):
        if re.search(r"(?<![a-zà-ÿ])" + v.lower() + r"(?![a-zà-ÿ])", texte):
            ville = v
            break
    if ville and ville in (lieux.get("villes") or {}):
        lat, lon = lieux["villes"][ville]
        annonce["lat"], annonce["lon"], annonce["position"] = lat, lon, f"{ville} (ville de l'agence)"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distances_hopital(annonce, lieux):
    h = (lieux or {}).get("hopital")
    if not h or annonce.get("lat") is None:
        return
    km = haversine_km(annonce["lat"], annonce["lon"], h["lat"], h["lon"]) * 1.3   # facteur route
    annonce["km_hopital"] = round(km, 1)
    annonce["min_velo"] = max(3, round(km / 15 * 60))
    annonce["min_voiture"] = max(3, round(km / 28 * 60))


def mediane(vals):
    v = sorted(vals)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def calculer_prix_m2(etat):
    """Compare le loyer au m² de chaque annonce à la médiane de sa ville (ou de l'ensemble)."""
    actives = [a for a in etat["annonces"].values() if a["statut"] == "active" and a.get("prix") and a.get("surface") and a["surface"] >= 20]
    for a in actives:
        a["prix_m2"] = round(a["prix"] / a["surface"], 1)
    par_ville = {}
    for a in actives:
        par_ville.setdefault(a.get("ville_agence"), []).append(a["prix_m2"])
    med_global = mediane([a["prix_m2"] for a in actives])
    for a in actives:
        vals = par_ville.get(a.get("ville_agence"), [])
        med = mediane(vals) if len(vals) >= 4 else med_global
        if not med:
            continue
        ecart = (a["prix_m2"] - med) / med * 100
        a["ecart_prix_pct"] = round(ecart)
        a["etiquette_prix"] = "bon prix" if ecart <= -10 else ("au-dessus du marché" if ecart >= 15 else "dans le marché")
    return {v: mediane(l) for v, l in par_ville.items()}, med_global


def tendances_7_jours(etat, aujourdhui):
    """Statistiques simples sur la semaine écoulée."""
    j0 = date.fromisoformat(aujourdhui)
    depuis = (j0 - timedelta(days=7)).isoformat()
    tous = list(etat["annonces"].values())
    sorties = [a for a in tous if a.get("premiere_vue", "") >= depuis and a["classement"] != "exclu"]
    disparues = [a for a in tous if a["statut"] == "disparu" and a.get("derniere_vue", "") >= depuis and a["classement"] != "exclu"]
    rapides = [a for a in disparues if (date.fromisoformat(a["derniere_vue"]) - date.fromisoformat(a["premiere_vue"])).days <= 3]
    actives = [a for a in tous if a["statut"] == "active" and a["classement"] != "exclu"]
    med_loyer = {}
    for v in ("Anglet", "Biarritz", "Bidart", "Bayonne"):
        l = [a["prix"] for a in actives if a.get("ville_agence") == v and a.get("prix")]
        if l:
            med_loyer[v] = (mediane(l), len(l))
    med_m2 = mediane([a["prix_m2"] for a in actives if a.get("prix_m2")])
    return {"sorties": len(sorties), "disparues": len(disparues), "rapides": len(rapides),
            "actives": len(actives), "med_loyer": med_loyer, "med_m2": med_m2, "depuis": depuis}


# --- Traitement d'une agence ----------------------------------------------------

def traiter_agence(agence):
    """Retourne (annonces, info) ; info = dict(statut, message, url_utilisee)."""
    url = agence["url"]
    html, err = recuperer(url)
    if html is None:
        return {}, {"statut": "erreur", "message": f"Page inaccessible ({err})", "url_utilisee": url}

    annonces = extraire_annonces(html, url, agence)
    url_utilisee = url

    # Si la page donnée est une accueil (peu d'annonces), tente la rubrique locations
    if len(annonces) < 2:
        page_loc = trouver_page_locations(html, url)
        if page_loc and normaliser_url(page_loc) != normaliser_url(url):
            time.sleep(1)
            html2, err2 = recuperer(page_loc)
            if html2:
                annonces2 = extraire_annonces(html2, page_loc, agence)
                if len(annonces2) > len(annonces):
                    annonces, url_utilisee = annonces2, page_loc

    if not annonces:
        msg = ("Aucun lien d'annonce reconnu : site probablement en JavaScript ou "
               "aucune location en ligne. Ouvrir l'URL à la main pour vérifier.")
        return {}, {"statut": "vide", "message": msg, "url_utilisee": url_utilisee}
    return annonces, {"statut": "ok", "message": f"{len(annonces)} annonce(s) détectée(s)",
                      "url_utilisee": url_utilisee}


# --- Mise à jour de l'état -------------------------------------------------------

def mettre_a_jour_etat(etat, annonces_du_jour, criteres, aujourdhui, scoring=None, lieux=None):
    """Fusionne les annonces trouvées dans l'état persistant. Retourne la liste des nouveautés."""
    nouveautes = []
    vues = set()
    for url_n, a in annonces_du_jour.items():
        vues.add(url_n)
        if url_n in etat["annonces"]:
            e = etat["annonces"][url_n]
            e["derniere_vue"] = aujourdhui
            e["statut"] = "active"
            e.pop("evenement", None)
            if e.get("prix") and not e.get("historique_prix"):
                if e.get("baisse"):
                    e["historique_prix"] = [{"date": e.get("premiere_vue", aujourdhui), "prix": e["baisse"]["ancien"]},
                                            {"date": e["baisse"]["date"], "prix": e["baisse"]["nouveau"]}]
                else:
                    e["historique_prix"] = [{"date": e.get("premiere_vue", aujourdhui), "prix": e["prix"]}]
            # changement de loyer ?
            if e.get("prix") and a.get("prix") and a["prix"] < e["prix"] * 0.97:
                e["baisse"] = {"ancien": e["prix"], "nouveau": a["prix"], "date": aujourdhui}
                e["prix"] = a["prix"]
                e["evenement"] = "baisse"
                e.setdefault("historique_prix", []).append({"date": aujourdhui, "prix": a["prix"]})
                nouveautes.append(e)
            elif e.get("prix") and a.get("prix") and a["prix"] > e["prix"] * 1.03:
                e["prix"] = a["prix"]
                e.setdefault("historique_prix", []).append({"date": aujourdhui, "prix": a["prix"]})
            # enrichit si on a mieux aujourd'hui
            for k in ("prix", "surface", "chambres", "pieces", "type"):
                if e.get(k) is None and a.get(k) is not None:
                    e[k] = a[k]
            if len(a.get("contexte", "")) > len(e.get("contexte", "")):
                e["contexte"], e["titre"] = a["contexte"], a["titre"]
            e["classement"] = classer(e, criteres)
            e["score"], e["atouts"], e["reserves"] = scorer(e, scoring)
        else:
            a = dict(a)
            a.update(premiere_vue=aujourdhui, derniere_vue=aujourdhui, statut="active")
            if a.get("prix"):
                a["historique_prix"] = [{"date": aujourdhui, "prix": a["prix"]}]
            a["classement"] = classer(a, criteres)
            a["score"], a["atouts"], a["reserves"] = scorer(a, scoring)
            etat["annonces"][url_n] = a
            nouveautes.append(a)

    # lecture des fiches détaillées des nouveautés (hors exclues), dans la limite quotidienne
    a_lire = [a for a in nouveautes if a["classement"] != "exclu" and not a.get("fiche_lue")]
    rattrapage = [a for a in etat["annonces"].values()
                  if a["statut"] == "active" and a["classement"] != "exclu"
                  and not a.get("photo") and not a.get("photo_cherchee") and a not in a_lire]
    a_lire += rattrapage
    if a_lire:
        log(f"    lecture de {min(len(a_lire), MAX_FICHES_PAR_PASSAGE)} fiche(s) détaillée(s)…")
    for a in a_lire[:MAX_FICHES_PAR_PASSAGE]:
        enrichir_par_fiche(a, criteres, scoring)
        a["photo_cherchee"] = True
        if a.get("prix") and not a.get("historique_prix"):
            a["historique_prix"] = [{"date": a.get("premiere_vue", aujourdhui), "prix": a["prix"]}]
        time.sleep(DELAI_ENTRE_FICHES)
    # rattrapage contact pour les fiches déjà lues sans téléphone (une fois)
    for a in [x for x in etat["annonces"].values() if x["statut"] == "active" and x["classement"] != "exclu"
              and x.get("fiche_lue") and not x.get("tel") and not x.get("contact_cherche")][:15]:
        f = lire_fiche(a["url"])
        if f["tels"]:
            a["tel"] = f["tels"][0]
        if f["mails"]:
            a["mail"] = f["mails"][0]
        a["contact_cherche"] = True
        time.sleep(DELAI_ENTRE_FICHES)
    for a in etat["annonces"].values():
        if a["statut"] == "active":
            geolocaliser(a, lieux)
            distances_hopital(a, lieux)
    calculer_prix_m2(etat)

    seuil_disparu = (date.fromisoformat(aujourdhui) - timedelta(days=JOURS_AVANT_DISPARU)).isoformat()
    seuil_purge = (date.fromisoformat(aujourdhui) - timedelta(days=JOURS_CONSERVATION_DISPARU)).isoformat()
    for url_n in list(etat["annonces"]):
        e = etat["annonces"][url_n]
        if url_n not in vues and e["derniere_vue"] < seuil_disparu:
            e["statut"] = "disparu"
        if e["statut"] == "disparu" and e["derniere_vue"] < seuil_purge:
            del etat["annonces"][url_n]
    return nouveautes


# --- Rapport HTML ----------------------------------------------------------------

def esc(s):
    return htmlmod.escape(str(s if s is not None else ""))


MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def date_courte(iso):
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {MOIS[d.month - 1]}"
    except Exception:
        return iso or ""


def prix_fmt(p):
    return f"{p:,} €".replace(",", " ") if p else None


def resume_bien(a):
    """Ligne courte : Appartement T4 · 80 m² · 3 ch."""
    parts = []
    if a.get("type"):
        parts.append(a["type"].capitalize())
    if a.get("pieces"):
        parts.append(f"T{a['pieces']}")
    if a.get("surface"):
        sv = a["surface"]
        parts.append(f"{int(sv) if float(sv).is_integer() else sv} m²")
    if a.get("chambres"):
        parts.append(f"{a['chambres']} ch.")
    return " · ".join(parts)


def libelle_champs(a):
    r = resume_bien(a)
    return (r + " — " + prix_fmt(a["prix"])) if a.get("prix") and r else (r or prix_fmt(a.get("prix")) or "")


def nettoyer_titre(a):
    t = (a.get("titre") or "").strip()
    t = re.sub(r"\s*\|.*$", "", t)                      # coupe "| 64100 827 € | 64 m²"
    t = re.sub(r"(\b\w+\b)(\s+\1\b)+", r"\1", t, flags=re.I)  # "Bayonne Bayonne" -> "Bayonne"
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—·")
    if len(t) > 90:
        t = t[:88].rsplit(" ", 1)[0] + "…"
    return t or a.get("url", "")


def classe_dpe(d):
    return {"A": "dpe-ab", "B": "dpe-ab", "C": "dpe-c", "D": "dpe-d", "E": "dpe-e"}.get(d, "dpe-fg")


def tel_affiche(t):
    t = re.sub(r"\D", "", t or "")
    if t.startswith("33") and len(t) == 11:
        t = "0" + t[2:]
    return " ".join(t[i:i + 2] for i in range(0, len(t), 2)) if len(t) == 10 else t


def tel_lien(t):
    t = re.sub(r"\D", "", t or "")
    if t.startswith("0") and len(t) == 10:
        return "+33" + t[1:]
    return "+" + t if not t.startswith("+") else t


def mailto(a, contact):
    from urllib.parse import quote
    if not contact:
        return None
    titre = nettoyer_titre(a)
    sujet = contact.get("sujet", "Location — {titre}").format(titre=titre, agence=a["agence"], url=a["url"])
    corps = contact.get("corps", "").format(titre=titre, agence=a["agence"], url=a["url"])
    dest = a.get("mail", "")
    return f"mailto:{dest}?subject={quote(sujet)}&body={quote(corps)}"


def texte_message(a, contact):
    if not contact:
        return ""
    titre = nettoyer_titre(a)
    return contact.get("corps", "").format(titre=titre, agence=a["agence"], url=a["url"])


def jours_en_ligne(a, aujourdhui=None):
    try:
        j0 = date.fromisoformat(aujourdhui) if aujourdhui else date.today()
        return (j0 - date.fromisoformat(a["premiere_vue"])).days
    except Exception:
        return None


def ligne_historique_prix(a):
    h = a.get("historique_prix") or []
    if len(h) < 2:
        return ""
    return "Loyer : " + " → ".join(f"{prix_fmt(x['prix'])} ({date_courte(x['date'])})" for x in h[-4:])


def ligne_distance(a):
    if a.get("km_hopital") is None:
        return ""
    return f"🏥 CH Côte Basque : ~{a['min_velo']} min à vélo · ~{a['min_voiture']} min en voiture ({a['km_hopital']} km)"


def carte_html(a, nouveau=False, seuil=5, contact=None):
    cl = a.get("classement", "a_verifier")
    score = a.get("score") or 0
    coeur = score >= seuil and cl != "exclu"
    prix = prix_fmt(a.get("prix"))
    pills = ""
    if a.get("pieces"):
        pills += f'<span class="pill">T{a["pieces"]}</span>'
    if a.get("chambres"):
        pills += f'<span class="pill">{a["chambres"]} ch.</span>'
    if a.get("surface"):
        sv = a["surface"]
        pills += f'<span class="pill">{int(sv) if float(sv).is_integer() else sv} m²</span>'
    if a.get("dpe"):
        pills += f'<span class="pill {classe_dpe(a["dpe"])}">DPE {a["dpe"]}</span>'
    if a.get("meuble"):
        pills += '<span class="pill">Meublé</span>'
    if a.get("etiquette_prix") and a.get("prix_m2"):
        cls_p = {"bon prix": "prix-bon", "au-dessus du marché": "prix-haut"}.get(a["etiquette_prix"], "prix-ok")
        signe = "+" if a.get("ecart_prix_pct", 0) > 0 else ""
        pills += f'<span class="pill {cls_p}">{a["prix_m2"]} €/m² · {a["etiquette_prix"]} ({signe}{a.get("ecart_prix_pct", 0)} %)</span>'
    j = jours_en_ligne(a)
    if j is not None and j >= 14:
        pills += f'<span class="pill {"longue" if j >= 21 else ""}">En ligne depuis {j} j{" · négociable ?" if j >= 21 else ""}</span>'
    if cl == "a_verifier":
        pills += '<span class="pill incertain">Infos à confirmer</span>'
    if cl == "exclu":
        pills += f'<span class="pill exclu">Écartée : {esc(a.get("motif_exclusion") or "critères")}</span>'
    atouts = "".join(f'<span class="chip plus">{esc(x)}</span>' for x in (a.get("atouts") or [])[:6])
    reserves = "".join(f'<span class="chip moins">{esc(x)}</span>' for x in (a.get("reserves") or [])[:4])
    photo = a.get("photo")
    bloc_photo = (f'<div class="photo" style="background-image:url(\'{esc(photo)}\')"></div>' if photo
                  else '<div class="photo vide"><span>Pas de photo</span></div>')
    badges = ""
    if a.get("evenement") == "baisse" and a.get("baisse"):
        b = a["baisse"]
        badges += f'<span class="badge baisse">Baisse −{prix_fmt(b["ancien"] - b["nouveau"])}</span>'
    elif nouveau:
        badges += '<span class="badge nouveau">Nouveau</span>'
    if coeur:
        badges += '<span class="badge coeur">♥ Coup de cœur</span>'
    ville = a.get("ville_agence", "")
    ident = re.sub(r"[^a-z0-9]", "", a["url"].lower())[-40:]
    tel = a.get("tel")
    lien_mail = mailto(a, contact)
    btn_tel = f'<a class="btn-sec" href="tel:{esc(tel_lien(tel))}">📞 {esc(tel_affiche(tel))}</a>' if tel else '<span class="btn-sec off">📞 n° sur la fiche</span>'
    btn_mail = f'<a class="btn-sec" href="{esc(lien_mail)}">✉️ E-mail{"" if a.get("mail") else " (à compléter)"}</a>' if lien_mail else ""
    dist = ligne_distance(a)
    pos = a.get("position")
    return f"""
<article class="carte {cl}{' coeur' if coeur else ''}{' nouveau' if nouveau else ''}" id="c-{ident}" data-id="{ident}" data-ville="{esc(ville)}" data-cl="{cl}" data-coeur="{1 if coeur else 0}" data-nouveau="{1 if nouveau else 0}">
  <a class="lien" href="{esc(a['url'])}" target="_blank" rel="noopener">
    {bloc_photo}
    <div class="badges">{badges}</div>
    <div class="corps">
      <div class="ligne-prix"><span class="prix">{esc(prix) if prix else '<span class="prix-inconnu">Loyer non lu</span>'}</span>{'<span class="par-mois">/ mois</span>' if prix else ''}<span class="score {'pos' if score > 0 else 'neg' if score < 0 else ''}">{score:+d}</span></div>
      <h3 class="titre">{esc(nettoyer_titre(a))}</h3>
      <div class="pills">{pills}</div>
      <div class="agence">{esc(a['agence'])} · {esc(ville)}{f' · {esc(pos)}' if pos else ''} · vu le {esc(date_courte(a.get('premiere_vue')))}</div>
      {f'<div class="dist">{esc(dist)}</div>' if dist else ''}
      {f'<div class="histo">📉 {esc(ligne_historique_prix(a))}</div>' if ligne_historique_prix(a) else ''}
      {f'<div class="chips">{atouts}{reserves}</div>' if atouts or reserves else ''}
    </div>
  </a>
  <div class="actions">
    <a class="btn" href="{esc(a['url'])}" target="_blank" rel="noopener">Voir l’annonce →</a>
    <div class="contact">{btn_tel}{btn_mail}<button class="btn-sec copier" data-msg="{esc(texte_message(a, contact))}">📋 Copier le message</button></div>
    <div class="suivi" data-id="{ident}">
      <button data-etat="vu">👁 Vu</button><button data-etat="contacte">📨 Contacté</button><button data-etat="visite">📅 Visite</button><button data-etat="non">✕ Non</button>
    </div>
  </div>
</article>"""


CSS_RAPPORT = """
:root { --encre:#1b1f1a; --papier:#f4f2ec; --carte:#ffffff; --vert:#1e5a3c; --vert-clair:#e4efe8; --rouge:#b5121b;
        --or:#c9a227; --bleu:#1f5f8b; --gris:#6b6f66; --gris-clair:#e6e3da; --ombre:0 1px 2px rgba(0,0,0,.06), 0 6px 18px rgba(0,0,0,.06); }
* { box-sizing:border-box; }
body { margin:0; background:var(--papier); color:var(--encre); font:16px/1.4 -apple-system, "Helvetica Neue", Arial, sans-serif; -webkit-text-size-adjust:100%; }
header { background:var(--vert); color:#fff; padding:14px 16px 10px; }
header h1 { margin:0; font-size:1.1rem; font-weight:700; }
header .sous { margin:3px 0 0; font-size:.82rem; opacity:.85; }
.onglets { position:sticky; top:0; z-index:500; display:flex; gap:6px; padding:10px 12px; background:var(--papier); border-bottom:1px solid var(--gris-clair); overflow-x:auto; }
.onglets button { flex:0 0 auto; border:1px solid var(--gris-clair); background:#fff; color:var(--encre); border-radius:20px; padding:7px 12px; font-size:.88rem; font-weight:600; cursor:pointer; }
.onglets button.actif { background:var(--vert); color:#fff; border-color:var(--vert); }
.onglets button .n { font-weight:400; opacity:.75; margin-left:4px; }
.filtres-villes { display:flex; gap:6px; padding:8px 12px 0; flex-wrap:wrap; }
.filtres-villes button { border:1px solid var(--gris-clair); background:transparent; border-radius:14px; padding:4px 10px; font-size:.8rem; color:var(--gris); cursor:pointer; }
.filtres-villes button.actif { color:var(--vert); border-color:var(--vert); background:var(--vert-clair); }
main { max-width:760px; margin:0 auto; padding:10px 12px 60px; }
.vide-msg { display:none; text-align:center; color:var(--gris); padding:40px 16px; font-size:.95rem; }
.grille { display:grid; grid-template-columns:1fr; gap:14px; }
@media (min-width:640px) { .grille { grid-template-columns:1fr 1fr; } }
.carte { background:var(--carte); border-radius:14px; overflow:hidden; box-shadow:var(--ombre); position:relative; border-left:5px solid transparent; }
.carte.s-vu { border-left-color:#b8b5aa; } .carte.s-contacte { border-left-color:var(--bleu); } .carte.s-visite { border-left-color:var(--or); } .carte.s-non { opacity:.5; }
.carte .lien { color:inherit; text-decoration:none; display:block; }
.carte .photo { height:170px; background:#dcd8cd center/cover no-repeat; }
.carte .photo.vide { display:flex; align-items:center; justify-content:center; color:#9a978e; font-size:.85rem; background:repeating-linear-gradient(45deg,#e6e3da 0 10px,#ece9e0 10px 20px); }
.carte .badges { position:absolute; top:10px; left:14px; display:flex; gap:6px; }
.badge { font-size:.72rem; font-weight:700; letter-spacing:.03em; text-transform:uppercase; padding:4px 8px; border-radius:6px; color:#fff; }
.badge.nouveau { background:var(--rouge); } .badge.coeur { background:var(--or); color:#2b2300; } .badge.baisse { background:var(--bleu); }
.carte .corps { padding:12px 14px 6px; }
.ligne-prix { display:flex; align-items:baseline; gap:6px; }
.prix { font-size:1.5rem; font-weight:800; color:var(--vert); }
.prix-inconnu { font-size:1rem; font-weight:600; color:var(--gris); }
.par-mois { font-size:.85rem; color:var(--gris); }
.score { margin-left:auto; font-size:.8rem; font-weight:700; padding:2px 8px; border-radius:10px; background:var(--gris-clair); color:var(--gris); }
.score.pos { background:var(--vert-clair); color:var(--vert); } .score.neg { background:#f3e4e4; color:#8a2a2e; }
.carte .titre { margin:6px 0 8px; font-size:1rem; font-weight:600; line-height:1.3; }
.pills { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }
.pill { font-size:.8rem; font-weight:600; padding:3px 9px; border-radius:8px; background:#f0eee7; color:var(--encre); }
.pill.dpe-ab { background:#d5efd9; color:#145a2a; } .pill.dpe-c { background:#e6f2c9; color:#3f5a11; }
.pill.dpe-d { background:#fff1c2; color:#6b5200; } .pill.dpe-e { background:#ffdfc2; color:#7a3d00; } .pill.dpe-fg { background:#ffd0d0; color:#7a1a1a; }
.pill.prix-bon { background:#d5efd9; color:#145a2a; } .pill.prix-ok { background:#f0eee7; color:var(--gris); font-weight:500; } .pill.prix-haut { background:#ffdfc2; color:#7a3d00; }
.pill.incertain { background:#fff7dc; color:#6b5200; font-weight:500; } .pill.exclu { background:#f3e4e4; color:#8a2a2e; font-weight:500; }
.agence { font-size:.8rem; color:var(--gris); margin-bottom:6px; }
.dist { font-size:.8rem; color:var(--encre); margin-bottom:6px; }
.histo { font-size:.8rem; color:var(--bleu); font-weight:600; margin-bottom:6px; }
.pill.longue { background:#fff1c2; color:#6b5200; }
.chips { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:6px; }
.chip { font-size:.74rem; padding:2px 8px; border-radius:10px; border:1px solid var(--gris-clair); color:var(--gris); }
.chip.plus { background:var(--vert-clair); border-color:#bfd6c8; color:var(--vert); } .chip.moins { background:#f7ecec; border-color:#e0c8c8; color:#8a2a2e; }
.actions { padding:6px 14px 12px; }
.btn { display:block; text-align:center; background:var(--vert); color:#fff; font-weight:700; padding:10px; border-radius:10px; font-size:.95rem; text-decoration:none; }
.contact { display:flex; gap:6px; margin-top:8px; flex-wrap:wrap; }
.btn-sec { flex:1 1 auto; text-align:center; border:1px solid var(--gris-clair); background:#fff; color:var(--encre); border-radius:9px; padding:8px 6px; font-size:.82rem; font-weight:600; text-decoration:none; cursor:pointer; white-space:nowrap; }
.btn-sec.off { color:#9a978e; cursor:default; }
.suivi { display:flex; gap:5px; margin-top:8px; }
.suivi button { flex:1; border:1px solid var(--gris-clair); background:#faf9f5; color:var(--gris); border-radius:8px; padding:6px 2px; font-size:.75rem; cursor:pointer; }
.suivi button.actif { background:var(--encre); color:#fff; border-color:var(--encre); }
.carte.exclu .btn { background:#8e938b; } .carte.exclu .prix { color:var(--gris); }
#carte-map { height:70vh; min-height:380px; border-radius:14px; overflow:hidden; box-shadow:var(--ombre); display:none; margin-top:4px; }
.prix-marker { background:var(--vert); color:#fff; font-weight:700; font-size:.78rem; padding:3px 7px; border-radius:8px; white-space:nowrap; box-shadow:0 1px 4px rgba(0,0,0,.3); border:2px solid #fff; }
.prix-marker.coeur { background:var(--or); color:#2b2300; } .prix-marker.hopital { background:var(--rouge); }
.leaflet-popup-content { font:14px/1.35 -apple-system, Arial, sans-serif; margin:10px 12px; }
.leaflet-popup-content a { color:var(--vert); font-weight:700; }
details.bloc { margin-top:24px; font-size:.85rem; background:#fff; border-radius:12px; padding:4px 12px; box-shadow:var(--ombre); }
details.bloc summary { cursor:pointer; padding:8px 0; font-weight:600; }
table { width:100%; border-collapse:collapse; }
td { border-top:1px solid var(--gris-clair); padding:6px 4px; vertical-align:top; }
tr.ok td:first-child { color:var(--vert); } tr.vide td:first-child, tr.erreur td:first-child { color:var(--rouge); }
.toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:var(--encre); color:#fff; padding:10px 16px; border-radius:20px; font-size:.85rem; display:none; z-index:999; }
footer { font-size:.78rem; color:var(--gris); margin-top:24px; line-height:1.5; }
"""

JS_RAPPORT = """
(function(){
  var onglet='nouveau', ville='toutes', carteInit=false, map=null, marqueurs=[];
  var cartes=[].slice.call(document.querySelectorAll('.carte'));
  var msg=document.getElementById('vide-msg'), grille=document.querySelector('.grille'), divMap=document.getElementById('carte-map');
  var toast=document.getElementById('toast');
  var suivi={}; try{ suivi=JSON.parse(localStorage.getItem('veille-suivi')||'{}'); }catch(e){}
  function sauver(){ try{ localStorage.setItem('veille-suivi', JSON.stringify(suivi)); }catch(e){} }
  function montrerToast(t){ toast.textContent=t; toast.style.display='block'; setTimeout(function(){toast.style.display='none';},1600); }
  function appliqueSuivi(c){
    var id=c.dataset.id, e=suivi[id];
    c.classList.remove('s-vu','s-contacte','s-visite','s-non'); if(e) c.classList.add('s-'+e);
    c.querySelectorAll('.suivi button').forEach(function(b){ b.classList.toggle('actif', b.dataset.etat===e); });
  }
  function visible(c){
    var ok=true, e=suivi[c.dataset.id];
    if(onglet==='nouveau') ok=c.dataset.nouveau==='1' && c.dataset.cl!=='exclu' && e!=='non';
    else if(onglet==='coeur') ok=c.dataset.coeur==='1' && e!=='non';
    else if(onglet==='tout'||onglet==='carte') ok=c.dataset.cl!=='exclu' && e!=='non';
    else if(onglet==='ecartees') ok=c.dataset.cl==='exclu' || e==='non';
    if(ok && ville!=='toutes' && c.dataset.ville!==ville) ok=false;
    return ok;
  }
  function applique(){
    var n=0;
    cartes.forEach(function(c){ var ok=visible(c); c.style.display=ok?'':'none'; if(ok) n++; appliqueSuivi(c); });
    var enCarte = onglet==='carte';
    grille.style.display = enCarte?'none':'';
    divMap.style.display = enCarte?'block':'none';
    msg.style.display=(n||enCarte)?'none':'';
    msg.textContent = onglet==='nouveau' ? 'Rien de nouveau depuis le dernier passage. Regarde « Coups de cœur » ou « Tout ».' : 'Aucune annonce dans cette sélection.';
    document.querySelectorAll('.onglets button').forEach(function(b){ b.classList.toggle('actif', b.dataset.onglet===onglet); });
    document.querySelectorAll('.filtres-villes button').forEach(function(b){ b.classList.toggle('actif', b.dataset.ville===ville); });
    if(enCarte) dessineCarte();
  }
  function dessineCarte(){
    if(!window.L){ divMap.innerHTML='<p style="padding:20px;color:#6b6f66">Carte indisponible hors connexion.</p>'; return; }
    if(!carteInit){
      map=L.map('carte-map',{scrollWheelZoom:false}).setView([43.478,-1.52],12);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'&copy; OpenStreetMap'}).addTo(map);
      if(window.HOPITAL){ L.marker([HOPITAL.lat,HOPITAL.lon],{icon:L.divIcon({className:'',html:'<div class="prix-marker hopital">🏥 CHCB</div>',iconAnchor:[30,12]})}).addTo(map).bindPopup('<b>'+HOPITAL.nom+'</b>'); }
      carteInit=true;
    }
    marqueurs.forEach(function(m){ map.removeLayer(m); }); marqueurs=[];
    var pts=[];
    (window.ANNONCES||[]).forEach(function(a){
      var c=document.getElementById('c-'+a.id); if(!c||!visible(c)) return;
      var lat=a.lat+(Math.random()-0.5)*0.003, lon=a.lon+(Math.random()-0.5)*0.004;
      var m=L.marker([lat,lon],{icon:L.divIcon({className:'',html:'<div class="prix-marker'+(a.coeur?' coeur':'')+'">'+(a.prix||'?')+'</div>',iconAnchor:[28,12]})}).addTo(map);
      m.bindPopup('<b>'+a.titre+'</b><br>'+(a.infos||'')+'<br><span style="color:#6b6f66;font-size:12px">'+a.agence+' · '+a.position+'</span><br><a href="'+a.url+'" target="_blank">Voir l’annonce →</a>');
      marqueurs.push(m); pts.push([lat,lon]);
    });
    setTimeout(function(){ map.invalidateSize(); if(pts.length) map.fitBounds(pts,{padding:[30,30],maxZoom:14}); },50);
  }
  document.querySelectorAll('.onglets button').forEach(function(b){ b.addEventListener('click', function(){ onglet=b.dataset.onglet; applique(); }); });
  document.querySelectorAll('.filtres-villes button').forEach(function(b){ b.addEventListener('click', function(){ ville=b.dataset.ville; applique(); }); });
  document.querySelectorAll('.suivi button').forEach(function(b){ b.addEventListener('click', function(){
    var id=b.parentNode.dataset.id; suivi[id] = (suivi[id]===b.dataset.etat)? undefined : b.dataset.etat; if(!suivi[id]) delete suivi[id]; sauver(); applique(); }); });
  document.querySelectorAll('.copier').forEach(function(b){ b.addEventListener('click', function(){
    var t=b.dataset.msg; if(navigator.clipboard){ navigator.clipboard.writeText(t).then(function(){ montrerToast('Message copié'); }); } else { montrerToast('Copie non disponible'); } }); });
  var nNouv=cartes.filter(function(c){return c.dataset.nouveau==='1' && c.dataset.cl!=='exclu';}).length;
  if(!nNouv) onglet='coeur';
  if(!cartes.filter(function(c){return c.dataset.coeur==='1';}).length && !nNouv) onglet='tout';
  applique();
})();
"""


def generer_rapport(etat, nouveautes, rapports_agences, criteres, aujourdhui, chemin, scoring=None, contact=None, lieux=None):
    seuil = (scoring or {}).get("coup_de_coeur_a_partir_de", 5)
    ordre_cl = {"match": 0, "a_verifier": 1, "exclu": 2}
    urls_nouv = {a["url"] for a in nouveautes}
    actives = [a for a in etat["annonces"].values() if a["statut"] == "active"]
    actives.sort(key=lambda a: (ordre_cl[a.get("classement", "a_verifier")], -(a.get("score") or 0), a.get("premiere_vue", "")))

    cartes = "".join(carte_html(a, a["url"] in urls_nouv, seuil, contact) for a in actives)
    n_nouv = sum(1 for a in nouveautes if a["classement"] != "exclu")
    n_coeur = sum(1 for a in actives if (a.get("score") or 0) >= seuil and a["classement"] != "exclu")
    n_tout = sum(1 for a in actives if a["classement"] != "exclu")
    n_exclu = len(actives) - n_tout
    villes = ["Anglet", "Biarritz", "Bidart", "Bayonne"]
    boutons_villes = '<button data-ville="toutes" class="actif">Toutes</button>' + "".join(
        f'<button data-ville="{v}">{v}</button>' for v in villes)

    # données carte
    donnees = []
    for a in actives:
        if a["classement"] == "exclu" or a.get("lat") is None:
            continue
        donnees.append({"id": re.sub(r"[^a-z0-9]", "", a["url"].lower())[-40:], "lat": a["lat"], "lon": a["lon"],
                        "prix": prix_fmt(a.get("prix")), "titre": nettoyer_titre(a), "infos": resume_bien(a),
                        "agence": a["agence"], "position": a.get("position", ""), "url": a["url"],
                        "coeur": (a.get("score") or 0) >= seuil})
    hopital = (lieux or {}).get("hopital")

    t = tendances_7_jours(etat, aujourdhui)
    lignes_med = "".join(f"<tr><td>{v}</td><td>{prix_fmt(round(m))}</td><td>{n} annonce(s)</td></tr>" for v, (m, n) in t["med_loyer"].items())
    bloc_tendances = f"""
<details class="bloc"><summary>📈 Tendances sur 7 jours</summary>
  <p>{t['sorties']} annonce(s) sortie(s) · {t['disparues']} retirée(s), dont {t['rapides']} en 3 jours ou moins · {t['actives']} en ligne aujourd’hui{f" · loyer médian {t['med_m2']} €/m²" if t['med_m2'] else ''}</p>
  <table>{lignes_med}</table>
</details>""" if t["med_loyer"] or t["sorties"] else ""

    lignes = "".join(
        f'<tr class="{r["statut"]}"><td>{esc(r["nom"])}</td><td>{esc(r["ville"])}</td><td>{esc(r["message"][:60])}</td>'
        f'<td><a href="{esc(r["url_utilisee"])}" target="_blank" rel="noopener">ouvrir</a></td></tr>' for r in rapports_agences)
    n_ok = sum(1 for r in rapports_agences if r["statut"] == "ok")
    c = criteres
    crit_txt = (f"{c.get('min_chambres', 2)} chambres min · {prix_fmt(c.get('max_loyer'))} max · "
                f"appartement ou maison · pas de RDC · à l’année")

    page = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille location Côte Basque — {esc(date_courte(aujourdhui))}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<style>{CSS_RAPPORT}</style></head>
<body>
<header>
  <h1>Veille location · Côte Basque</h1>
  <p class="sous">{esc(date_courte(aujourdhui))} · {n_tout} annonces en ligne · {n_ok} agences lues</p>
  <p class="sous">{esc(crit_txt)}</p>
</header>
<nav class="onglets">
  <button data-onglet="nouveau">Nouveau<span class="n">{n_nouv}</span></button>
  <button data-onglet="coeur">♥ Coups de cœur<span class="n">{n_coeur}</span></button>
  <button data-onglet="tout">Tout<span class="n">{n_tout}</span></button>
  <button data-onglet="carte">🗺 Carte</button>
  <button data-onglet="ecartees">Écartées<span class="n">{n_exclu}</span></button>
</nav>
<div class="filtres-villes">{boutons_villes}</div>
<main>
  <p id="vide-msg" class="vide-msg"></p>
  <div id="carte-map"></div>
  <div class="grille">{cartes}</div>
  {bloc_tendances}
  <details class="bloc"><summary>État des {len(rapports_agences)} agences surveillées</summary><table>{lignes}</table></details>
  <footer>Score = mots-clés positifs (vert) et négatifs (rouge) lus dans l’annonce + bonus surface / loyer / DPE ; ♥ à partir de {seuil}.
  Positions sur la carte approximatives (quartier cité, sinon ville de l’agence). Temps vers l’hôpital estimés à vol d’oiseau × 1,3.
  Les boutons Vu / Contacté / Visite / Non sont mémorisés sur cet appareil. Annonces retirées des sites depuis {JOURS_AVANT_DISPARU} jours supprimées.
  Leboncoin, SeLoger, Bien’ici, Foncia, Laforêt ne sont pas lus ici : alertes e-mail + Jinka.</footer>
</main>
<div id="toast" class="toast"></div>
<script>window.ANNONCES = {json.dumps(donnees, ensure_ascii=False)}; window.HOPITAL = {json.dumps(hopital, ensure_ascii=False) if hopital else 'null'};</script>
<script>{JS_RAPPORT}</script>
</body></html>"""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(page, encoding="utf-8")


# --- E-mail ------------------------------------------------------------------------

def carte_mail(a, seuil=5, contact=None):
    """Version e-mail d'une carte : tableau + styles en ligne (les clients mail ignorent le CSS)."""
    score = a.get("score") or 0
    coeur = score >= seuil
    prix = prix_fmt(a.get("prix")) or "Loyer non lu"
    infos = resume_bien(a)
    if a.get("dpe"):
        infos += f" · DPE {a['dpe']}"
    if a.get("meuble"):
        infos += " · meublé"
    atouts = " · ".join((a.get("atouts") or [])[:5])
    reserves = " · ".join((a.get("reserves") or [])[:3])
    photo = f'<img src="{esc(a["photo"])}" width="100%" style="display:block;max-height:220px;object-fit:cover;border-radius:10px 10px 0 0" alt="">' if a.get("photo") else ""
    etiquette = '<span style="background:#c9a227;color:#2b2300;font-size:11px;font-weight:700;padding:3px 7px;border-radius:5px;margin-right:6px">♥ COUP DE CŒUR</span>' if coeur else ""
    if a.get("evenement") == "baisse" and a.get("baisse"):
        etiquette = f'<span style="background:#1f5f8b;color:#fff;font-size:11px;font-weight:700;padding:3px 7px;border-radius:5px;margin-right:6px">BAISSE −{esc(prix_fmt(a["baisse"]["ancien"] - a["baisse"]["nouveau"]))}</span>' + etiquette
    if a.get("etiquette_prix") == "bon prix":
        infos += " · bon prix"
    dist = ligne_distance(a)
    j = jours_en_ligne(a)
    if j is not None and j >= 14:
        infos += f" · en ligne depuis {j} j"
    histo = ligne_historique_prix(a)
    if histo:
        dist = (histo + ("  ·  " + dist if dist else ""))
    btns = ""
    if a.get("tel"):
        btns += f'<a href="tel:{esc(tel_lien(a["tel"]))}" style="display:inline-block;border:1px solid #cfcbc0;color:#1b1f1a;text-decoration:none;font-weight:600;padding:8px 12px;border-radius:8px;font-size:13px;margin:6px 6px 0 0">📞 {esc(tel_affiche(a["tel"]))}</a>'
    lm = mailto(a, contact)
    if lm:
        btns += f'<a href="{esc(lm)}" style="display:inline-block;border:1px solid #cfcbc0;color:#1b1f1a;text-decoration:none;font-weight:600;padding:8px 12px;border-radius:8px;font-size:13px;margin:6px 0 0">✉️ E-mail pré-rédigé</a>'
    return f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#fff;border-radius:10px;margin:0 0 14px;border:1px solid #e6e3da;font-family:-apple-system,Helvetica,Arial,sans-serif">
  <tr><td>{photo}</td></tr>
  <tr><td style="padding:12px 14px 14px">
    <div style="margin-bottom:4px">{etiquette}<span style="font-size:22px;font-weight:800;color:#1e5a3c">{esc(prix)}</span> <span style="color:#6b6f66;font-size:13px">/ mois</span></div>
    <div style="font-size:16px;font-weight:600;color:#1b1f1a;margin:4px 0">{esc(nettoyer_titre(a))}</div>
    <div style="font-size:14px;color:#1b1f1a;margin-bottom:4px">{esc(infos)}</div>
    <div style="font-size:12px;color:#6b6f66;margin-bottom:8px">{esc(a['agence'])} · {esc(a.get('ville_agence',''))}</div>
    {f'<div style="font-size:12px;color:#1e5a3c;margin-bottom:3px">+ {esc(atouts)}</div>' if atouts else ''}
    {f'<div style="font-size:12px;color:#8a2a2e;margin-bottom:8px">− {esc(reserves)}</div>' if reserves else ''}
    {f'<div style="font-size:12px;color:#1b1f1a;margin-bottom:8px">{esc(dist)}</div>' if dist else ''}
    <a href="{esc(a['url'])}" style="display:inline-block;background:#1e5a3c;color:#fff;text-decoration:none;font-weight:700;padding:9px 16px;border-radius:8px;font-size:14px">Voir l’annonce →</a>
    <div>{btns}</div>
  </td></tr>
</table>"""


def generer_mail(etat, nouveautes, aujourdhui, url_rapport, seuil=5, chemin_html=None, chemin_sujet=None, contact=None, hebdo=False):
    """Écrit le corps HTML de l'e-mail et son sujet. Retourne (sujet, html)."""
    nouv = sorted([a for a in nouveautes if a["classement"] != "exclu"], key=lambda a: -(a.get("score") or 0))
    actives = [a for a in etat["annonces"].values() if a["statut"] == "active" and a["classement"] != "exclu"]
    top = sorted(actives, key=lambda a: -(a.get("score") or 0))[:4]
    n_coeur = sum(1 for a in nouv if (a.get("score") or 0) >= seuil)
    n_b = sum(1 for a in nouv if a.get("evenement") == "baisse")
    n_n = len(nouv) - n_b
    if nouv:
        morceaux = []
        if n_n:
            morceaux.append(f"{n_n} nouvelle{'s' if n_n > 1 else ''} annonce{'s' if n_n > 1 else ''}")
        if n_b:
            morceaux.append(f"{n_b} baisse{'s' if n_b > 1 else ''} de loyer")
        sujet = ("🏠 " if n_n else "📉 ") + " + ".join(morceaux) + (f", dont {n_coeur} coup{'s' if n_coeur > 1 else ''} de cœur" if n_coeur else "") + f" — {date_courte(aujourdhui)}"
    else:
        sujet = f"Veille location — rien de nouveau le {date_courte(aujourdhui)}"
    bloc_nouv = "".join(carte_mail(a, seuil, contact) for a in nouv) if nouv else \
        '<p style="color:#6b6f66;font-family:Helvetica,Arial,sans-serif">Aucune nouvelle annonce sur les agences surveillées depuis hier.</p>'
    bloc_top = "".join(carte_mail(a, seuil, contact) for a in top if a not in nouv)
    bloc_hebdo = ""
    if hebdo:
        t = tendances_7_jours(etat, aujourdhui)
        lignes = "".join(f'<tr><td style="padding:4px 0">{v}</td><td style="padding:4px 8px"><b>{prix_fmt(round(m))}</b></td><td style="padding:4px 0;color:#6b6f66">{n} annonce(s)</td></tr>' for v, (m, n) in t["med_loyer"].items())
        bloc_hebdo = f"""<h2 style="font-size:13px;color:#6b6f66;text-transform:uppercase;letter-spacing:.06em;margin:22px 0 10px">📈 Le marché cette semaine</h2>
  <div style="background:#fff;border:1px solid #e6e3da;border-radius:10px;padding:12px 14px;font-size:14px">
  <p style="margin:0 0 8px">{t['sorties']} annonce(s) sortie(s) sur les agences suivies · {t['disparues']} retirée(s), dont <b>{t['rapides']} parties en 3 jours ou moins</b> · {t['actives']} en ligne aujourd’hui{f" · médiane {t['med_m2']} €/m²" if t['med_m2'] else ''}</p>
  <table style="border-collapse:collapse;font-size:13px">{lignes}</table></div>"""
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;background:#f4f2ec;padding:16px 10px">
<div style="max-width:560px;margin:0 auto;font-family:-apple-system,Helvetica,Arial,sans-serif">
  <h1 style="font-size:18px;color:#1e5a3c;margin:0 0 4px">Veille location · Côte Basque</h1>
  <p style="font-size:13px;color:#6b6f66;margin:0 0 16px">{esc(date_courte(aujourdhui))} · <a href="{esc(url_rapport)}" style="color:#1e5a3c">ouvrir le rapport complet</a></p>
  <h2 style="font-size:13px;color:#6b6f66;text-transform:uppercase;letter-spacing:.06em;margin:0 0 10px">Nouveau aujourd’hui ({len(nouv)})</h2>
  {bloc_nouv}
  {f'<h2 style="font-size:13px;color:#6b6f66;text-transform:uppercase;letter-spacing:.06em;margin:22px 0 10px">Toujours en ligne, les mieux notées</h2>{bloc_top}' if bloc_top else ''}
  {bloc_hebdo}
  <p style="font-size:12px;color:#6b6f66;margin-top:20px">Envoyé automatiquement chaque matin. Portails nationaux non inclus : Jinka + alertes Foncia / Laforêt / Human.</p>
</div></body></html>"""
    if chemin_html:
        chemin_html.parent.mkdir(parents=True, exist_ok=True)
        chemin_html.write_text(html, encoding="utf-8")
    if chemin_sujet:
        chemin_sujet.write_text(sujet, encoding="utf-8")
    return sujet, html


def envoyer_mail(sujet, html):
    """Envoie via SMTP si MAIL_USER / MAIL_PASSWORD / MAIL_TO sont définis dans l'environnement."""
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    user, pwd, to = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASSWORD"), os.environ.get("MAIL_TO")
    if not (user and pwd and to):
        log("E-mail non envoyé : MAIL_USER / MAIL_PASSWORD / MAIL_TO absents.")
        return False
    serveur = os.environ.get("MAIL_SMTP", "smtp.gmail.com")
    port = int(os.environ.get("MAIL_PORT", "465"))
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = sujet, f"Veille location <{user}>", to
    msg.attach(MIMEText("Rapport disponible en ligne.", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    destinataires = [d.strip() for d in re.split(r"[,;]", to) if d.strip()]
    try:
        with smtplib.SMTP_SSL(serveur, port, timeout=30) as smtp:
            smtp.login(user, pwd)
            smtp.sendmail(user, destinataires, msg.as_string())
        log(f"E-mail envoyé à {', '.join(destinataires)}.")
        return True
    except Exception as e:
        log(f"Échec de l'envoi e-mail : {type(e).__name__} : {e}")
        return False


# --- Programme principal ----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Veille location Côte Basque")
    ap.add_argument("--test", action="store_true", help="vérifie les agences sans rien enregistrer")
    ap.add_argument("--agence", default=None, help="ne traite que les agences dont le nom contient ce texte")
    ap.add_argument("--sortie", default=str(SORTIE_DEFAUT), help="chemin du rapport HTML")
    ap.add_argument("--config", default=str(FICHIER_CONFIG))
    ap.add_argument("--etat", default=str(FICHIER_ETAT))
    ap.add_argument("--mail", action="store_true", help="envoie le digest par e-mail (variables MAIL_USER, MAIL_PASSWORD, MAIL_TO)")
    ap.add_argument("--hebdo", action="store_true", help="force le bloc tendances 7 jours dans l'e-mail (sinon le lundi)")
    ap.add_argument("--mail-toujours", action="store_true", help="envoie l'e-mail même sans nouveauté")
    ap.add_argument("--url-rapport", default="https://marcantoinezipoli.github.io/veille-location-basque/")
    args = ap.parse_args()

    config = charger_json(Path(args.config), None)
    if not config:
        log(f"Fichier de configuration introuvable : {args.config}")
        sys.exit(1)
    criteres = config.get("criteres", {})
    scoring = config.get("scoring", {})
    contact = config.get("contact", {})
    lieux = config.get("lieux", {})
    agences = config["agences"]
    if args.agence:
        agences = [a for a in agences if args.agence.lower() in a["nom"].lower()]
        if not agences:
            log("Aucune agence ne correspond à ce filtre.")
            sys.exit(1)

    aujourdhui = date.today().isoformat()
    etat = charger_json(Path(args.etat), {"annonces": {}, "historique": []})

    toutes = {}
    rapports = []
    for i, ag in enumerate(agences):
        log(f"[{i+1}/{len(agences)}] {ag['nom']} …")
        annonces, info = traiter_agence(ag)
        rapports.append({"nom": ag["nom"], "ville": ag["ville"], **info})
        log(f"    -> {info['statut']} : {info['message']}")
        if args.test:
            for a in list(annonces.values())[:5]:
                log(f"       · {libelle_champs(a) or '?'} | {a['titre'][:70]} | {a['url']}")
            if len(annonces) > 5:
                log(f"       … et {len(annonces)-5} autre(s)")
        toutes.update(annonces)
        if i < len(agences) - 1:
            time.sleep(DELAI_ENTRE_SITES)

    if args.test:
        n_ok = sum(1 for r in rapports if r["statut"] == "ok")
        log(f"\nTest terminé : {n_ok}/{len(rapports)} agences lisibles, {len(toutes)} liens d'annonce au total.")
        log("Rien n'a été enregistré. Lancer sans --test pour créer l'état initial et le rapport.")
        return

    nouveautes = mettre_a_jour_etat(etat, toutes, criteres, aujourdhui, scoring, config.get("lieux"))
    premier_lancement = not etat.get("historique")
    etat.setdefault("historique", []).append(
        {"date": aujourdhui, "nouveautes": len(nouveautes), "actives": sum(1 for a in etat["annonces"].values() if a["statut"] == "active"),
         "agences_ok": sum(1 for r in rapports if r["statut"] == "ok")})
    etat["historique"] = etat["historique"][-400:]
    sauver_json(Path(args.etat), etat)

    if premier_lancement:
        # au premier passage tout est "nouveau" : on le signale mais on ne surligne pas 300 annonces
        log(f"\nPremier lancement : {len(nouveautes)} annonces enregistrées comme base de référence.")
        nouveautes_affichees = []
    else:
        nouveautes_affichees = nouveautes

    generer_rapport(etat, nouveautes_affichees, rapports, criteres, aujourdhui, Path(args.sortie), scoring, contact, lieux)
    seuil = scoring.get("coup_de_coeur_a_partir_de", 5)
    dossier_sortie = Path(args.sortie).parent
    hebdo = args.hebdo or date.fromisoformat(aujourdhui).weekday() == 0
    sujet, html_mail = generer_mail(etat, nouveautes_affichees, aujourdhui, args.url_rapport, seuil,
                                    dossier_sortie / "mail.html", dossier_sortie / "mail_sujet.txt", contact, hebdo)
    if args.mail:
        heure_utc = datetime.utcnow().hour
        if nouveautes_affichees or heure_utc < 9 or args.mail_toujours:
            envoyer_mail(sujet, html_mail)
        else:
            log("E-mail non envoyé : rien de nouveau à ce passage (le digest du matin part toujours).")
    n_match = sum(1 for a in nouveautes if a["classement"] == "match")
    log(f"\nRapport écrit : {args.sortie}")
    log(f"{len(nouveautes)} nouveauté(s) dont {n_match} correspondant aux critères.")


if __name__ == "__main__":
    main()
