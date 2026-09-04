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
        zone1 = (annonce.get("titre", "") + " " + annonce.get("url", "") + " " + annonce.get("contexte", "")).lower()
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
        return None
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "nav", "header", "footer", "noscript", "svg", "form"]):
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
    return texte or None


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
    texte = lire_fiche(annonce["url"])
    annonce["fiche_lue"] = bool(texte)
    if texte:
        annonce["description"] = texte[:1200]
        prix, surface, chambres, pieces, typ = extraire_champs(texte)
        for k, v in (("prix", prix), ("surface", surface), ("chambres", chambres), ("pieces", pieces), ("type", typ)):
            if annonce.get(k) is None and v is not None:
                annonce[k] = v
        annonce["dpe"] = extraire_dpe(texte) or annonce.get("dpe")
        annonce["meuble"] = bool(RE_MEUBLE.search(texte))
        annonce["classement"] = classer(annonce, criteres)
    annonce["score"], annonce["atouts"], annonce["reserves"] = scorer(annonce, scoring)


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

def mettre_a_jour_etat(etat, annonces_du_jour, criteres, aujourdhui, scoring=None):
    """Fusionne les annonces trouvées dans l'état persistant. Retourne la liste des nouveautés."""
    nouveautes = []
    vues = set()
    for url_n, a in annonces_du_jour.items():
        vues.add(url_n)
        if url_n in etat["annonces"]:
            e = etat["annonces"][url_n]
            e["derniere_vue"] = aujourdhui
            e["statut"] = "active"
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
            a["classement"] = classer(a, criteres)
            a["score"], a["atouts"], a["reserves"] = scorer(a, scoring)
            etat["annonces"][url_n] = a
            nouveautes.append(a)

    # lecture des fiches détaillées des nouveautés (hors exclues), dans la limite quotidienne
    a_lire = [a for a in nouveautes if a["classement"] != "exclu" and not a.get("fiche_lue")]
    if a_lire:
        log(f"    lecture de {min(len(a_lire), MAX_FICHES_PAR_PASSAGE)} fiche(s) détaillée(s)…")
    for a in a_lire[:MAX_FICHES_PAR_PASSAGE]:
        enrichir_par_fiche(a, criteres, scoring)
        time.sleep(DELAI_ENTRE_FICHES)

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


def libelle_champs(a):
    parts = []
    if a.get("type"):
        parts.append(a["type"].capitalize())
    if a.get("pieces"):
        parts.append(f"T{a['pieces']}")
    if a.get("chambres"):
        parts.append(f"{a['chambres']} ch.")
    if a.get("surface"):
        s = a["surface"]
        parts.append(f"{int(s) if float(s).is_integer() else s} m²")
    if a.get("prix"):
        parts.append(f"{a['prix']:,} €".replace(",", " "))
    return " — ".join(parts)


def carte_annonce(a, nouveau=False, seuil_coeur=5):
    cl = a.get("classement", "a_verifier")
    badge = {"match": "Correspond", "a_verifier": "À vérifier", "exclu": "Hors critères"}[cl]
    if cl == "exclu" and a.get("motif_exclusion"):
        badge += f" ({a['motif_exclusion']})"
    infos = libelle_champs(a)
    if a.get("dpe"):
        infos += f" — DPE {a['dpe']}"
    if a.get("meuble"):
        infos += " — meublé"
    ctx = a.get("description") or a.get("contexte", "")
    titre = a.get("titre") or a["url"]
    score = a.get("score", 0) or 0
    coeur = score >= seuil_coeur and cl != "exclu"
    chips = "".join(f'<span class="chip plus">{esc(x)}</span>' for x in (a.get("atouts") or [])[:8])
    chips += "".join(f'<span class="chip moins">{esc(x)}</span>' for x in (a.get("reserves") or [])[:5])
    return f"""
    <li class="annonce {cl}{' nouveau' if nouveau else ''}{' coeur' if coeur else ''}">
      <a href="{esc(a['url'])}" target="_blank" rel="noopener">
        <span class="ligne1"><span class="titre">{esc(titre[:110])}</span><span class="score" title="score mots-clés">{'♥ ' if coeur else ''}{score:+d}</span></span>
        <span class="infos">{esc(infos) if infos else '<em>caractéristiques non lues, ouvrir l’annonce</em>'}</span>
        <span class="meta">{esc(a['agence'])} · {esc(a.get('ville_agence',''))} · vu le {esc(a.get('premiere_vue',''))} · {badge}</span>
      </a>
      {f'<div class="chips">{chips}</div>' if chips else ''}
      {f'<p class="ctx">{esc(ctx[:240])}…</p>' if ctx and ctx != titre else ''}
    </li>"""


def generer_rapport(etat, nouveautes, rapports_agences, criteres, aujourdhui, chemin, scoring=None):
    seuil = (scoring or {}).get("coup_de_coeur_a_partir_de", 5)
    actives = [a for a in etat["annonces"].values() if a["statut"] == "active"]
    actives.sort(key=lambda a: (
        {"match": 0, "a_verifier": 1, "exclu": 2}[a.get("classement", "a_verifier")],
        -(a.get("score") or 0), a.get("premiere_vue", "")), reverse=False)
    actives.sort(key=lambda a: -(a.get("score") or 0) if a.get("classement") != "exclu" else 10**6)
    actives.sort(key=lambda a: {"match": 0, "a_verifier": 1, "exclu": 2}[a.get("classement", "a_verifier")])
    nouveautes = sorted(nouveautes, key=lambda a: -(a.get("score") or 0))

    nouv_match = [a for a in nouveautes if a["classement"] == "match"]
    nouv_verif = [a for a in nouveautes if a["classement"] == "a_verifier"]
    nouv_exclu = [a for a in nouveautes if a["classement"] == "exclu"]

    def liste(items, nouveau=False):
        return "<ul class='liste'>" + "".join(carte_annonce(a, nouveau, seuil) for a in items) + "</ul>" if items else ""

    # groupe des actives par ville
    par_ville = {}
    for a in actives:
        par_ville.setdefault(a.get("ville_agence", "Autre"), []).append(a)

    sections_villes = ""
    for ville in ["Anglet", "Biarritz", "Bidart", "Bayonne"] + sorted(set(par_ville) - {"Anglet", "Biarritz", "Bidart", "Bayonne"}):
        if ville not in par_ville:
            continue
        items = par_ville[ville]
        visibles = [a for a in items if a["classement"] != "exclu"]
        exclues = [a for a in items if a["classement"] == "exclu"]
        sections_villes += f"""
        <details class="ville" open>
          <summary>{esc(ville)} <span class="compte">{len(visibles)} annonce(s)</span></summary>
          {liste(visibles)}
          {f'<details class="exclues"><summary>{len(exclues)} hors critères (étudiant, studio, parking, hors budget…)</summary>{liste(exclues)}</details>' if exclues else ''}
        </details>"""

    lignes_agences = ""
    for r in rapports_agences:
        cls = r["statut"]
        lignes_agences += f"""<tr class="{cls}"><td>{esc(r['nom'])}</td><td>{esc(r['ville'])}</td>
        <td>{esc(r['message'])}</td><td><a href="{esc(r['url_utilisee'])}" target="_blank" rel="noopener">ouvrir</a></td></tr>"""

    n_ok = sum(1 for r in rapports_agences if r["statut"] == "ok")
    n_pb = len(rapports_agences) - n_ok
    c = criteres
    crit_txt = (f"{c.get('min_chambres', '?')} chambres minimum, {c.get('max_loyer', '?')} € CC maximum, "
                f"appartement ou maison, pas de rez-de-chaussée, location à l'année uniquement")

    if nouveautes:
        bloc_nouv = f"""
        <section class="nouveautes">
          <h2>Nouveau aujourd’hui <span class="compte">{len(nouveautes)}</span></h2>
          {f'<h3>Correspond à vos critères ({len(nouv_match)})</h3>' + liste(nouv_match, True) if nouv_match else ''}
          {f'<h3>À vérifier ({len(nouv_verif)})</h3><p class="note">Caractéristiques incomplètes sur la page de liste : ouvrir l’annonce pour trancher.</p>' + liste(nouv_verif, True) if nouv_verif else ''}
          {f'<details class="exclues"><summary>{len(nouv_exclu)} nouveauté(s) hors critères</summary>{liste(nouv_exclu, True)}</details>' if nouv_exclu else ''}
        </section>"""
    else:
        bloc_nouv = """<section class="nouveautes vide"><h2>Nouveau aujourd’hui</h2>
        <p>Rien de nouveau depuis le dernier passage. Les annonces actives restent listées ci-dessous.</p></section>"""

    page = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille location Côte Basque — {esc(aujourdhui)}</title>
<style>
:root {{ --encre:#1c1f1a; --papier:#fbfaf6; --vert:#1e5a3c; --rouge:#b5121b; --gris:#6b6f66; --ligne:#dcdad2; --jaune:#f6ead1; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--papier); color:var(--encre); font:16px/1.45 "Helvetica Neue", Arial, sans-serif; }}
header {{ background:var(--vert); color:#fff; padding:18px 16px 14px; }}
header h1 {{ margin:0; font-size:1.25rem; font-weight:600; }}
header p {{ margin:4px 0 0; opacity:.85; font-size:.9rem; }}
main {{ max-width:720px; margin:0 auto; padding:8px 12px 48px; }}
h2 {{ font-size:1.1rem; margin:22px 0 8px; display:flex; align-items:baseline; gap:8px; }}
h3 {{ font-size:.95rem; margin:14px 0 6px; color:var(--vert); }}
.compte {{ font-size:.8rem; font-weight:normal; color:var(--gris); }}
.note {{ font-size:.85rem; color:var(--gris); margin:0 0 6px; }}
.liste {{ list-style:none; margin:0; padding:0; }}
.annonce {{ border-top:1px solid var(--ligne); padding:10px 0; }}
.annonce a {{ text-decoration:none; color:inherit; display:block; }}
.annonce a span {{ display:block; margin-top:2px; }}
.annonce .titre {{ font-weight:600; }}
.annonce .infos {{ font-size:.95rem; }}
.annonce .meta {{ font-size:.8rem; color:var(--gris); }}
.annonce .badge {{ font-size:.75rem; color:var(--gris); }}
.annonce.match .badge {{ color:var(--vert); font-weight:600; }}
.annonce.nouveau {{ background:var(--jaune); padding-left:10px; padding-right:10px; border-left:4px solid var(--rouge); }}
.annonce .ctx {{ margin:6px 0 0; font-size:.82rem; color:var(--gris); }}
.annonce .ligne1 {{ display:flex; justify-content:space-between; gap:8px; align-items:baseline; }}
.annonce .score {{ font-size:.85rem; color:var(--gris); white-space:nowrap; }}
.annonce.coeur .score {{ color:var(--rouge); font-weight:600; }}
.annonce.coeur .titre {{ color:var(--vert); }}
.chips {{ margin-top:6px; display:flex; flex-wrap:wrap; gap:4px; }}
.chip {{ font-size:.75rem; padding:1px 7px; border-radius:10px; border:1px solid var(--ligne); }}
.chip.plus {{ background:#e6f0ea; border-color:#bfd6c8; color:var(--vert); }}
.chip.moins {{ background:#f3ecec; border-color:#dcc3c3; color:#7a2d31; }}
details.ville {{ margin-top:10px; }}
details.ville > summary {{ font-size:1.05rem; font-weight:600; cursor:pointer; padding:8px 0; }}
details.exclues {{ margin:8px 0 0; }}
details.exclues > summary {{ font-size:.85rem; color:var(--gris); cursor:pointer; }}
details.exclues .annonce {{ opacity:.7; }}
table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
td {{ border-top:1px solid var(--ligne); padding:6px 4px; vertical-align:top; }}
tr.ok td:first-child {{ color:var(--vert); }}
tr.vide td:first-child, tr.erreur td:first-child {{ color:var(--rouge); }}
.vide p {{ color:var(--gris); }}
footer {{ font-size:.8rem; color:var(--gris); margin-top:28px; }}
</style></head>
<body>
<header>
  <h1>Veille location — Anglet · Biarritz · Bidart · Bayonne</h1>
  <p>Mis à jour le {esc(aujourdhui)} · {len(actives)} annonces actives · {n_ok} agences lues{f' · {n_pb} à vérifier' if n_pb else ''}</p>
  <p>Critères : {esc(crit_txt)} · score ≥ {seuil} = coup de cœur ♥</p>
</header>
<main>
  {bloc_nouv}
  <h2>Toutes les annonces actives</h2>
  <p class="note">Par ville d’implantation de l’agence, triées par score. Le score additionne les mots-clés positifs (vert) et négatifs (rouge) trouvés dans l’annonce, plus des bonus surface / loyer / DPE. Les annonces non revues depuis {JOURS_AVANT_DISPARU} jours sont retirées automatiquement.</p>
  {sections_villes or '<p class="note">Aucune annonce active pour l’instant.</p>'}
  <h2>État des agences surveillées</h2>
  <table>{lignes_agences}</table>
  <footer>Les portails nationaux (Leboncoin, SeLoger, Bien’ici, PAP) ne sont pas lus par cet outil : utiliser leurs alertes e-mail ou Jinka en complément.</footer>
</main>
</body></html>"""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(page, encoding="utf-8")


# --- Programme principal ----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Veille location Côte Basque")
    ap.add_argument("--test", action="store_true", help="vérifie les agences sans rien enregistrer")
    ap.add_argument("--agence", default=None, help="ne traite que les agences dont le nom contient ce texte")
    ap.add_argument("--sortie", default=str(SORTIE_DEFAUT), help="chemin du rapport HTML")
    ap.add_argument("--config", default=str(FICHIER_CONFIG))
    ap.add_argument("--etat", default=str(FICHIER_ETAT))
    args = ap.parse_args()

    config = charger_json(Path(args.config), None)
    if not config:
        log(f"Fichier de configuration introuvable : {args.config}")
        sys.exit(1)
    criteres = config.get("criteres", {})
    scoring = config.get("scoring", {})
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

    nouveautes = mettre_a_jour_etat(etat, toutes, criteres, aujourdhui, scoring)
    premier_lancement = not etat.get("historique")
    etat.setdefault("historique", []).append(
        {"date": aujourdhui, "nouveautes": len(nouveautes), "actives": sum(1 for a in etat["annonces"].values() if a["statut"] == "active"),
         "agences_ok": sum(1 for r in rapports if r["statut"] == "ok")})
    etat["historique"] = etat["historique"][-90:]
    sauver_json(Path(args.etat), etat)

    if premier_lancement:
        # au premier passage tout est "nouveau" : on le signale mais on ne surligne pas 300 annonces
        log(f"\nPremier lancement : {len(nouveautes)} annonces enregistrées comme base de référence.")
        nouveautes_affichees = []
    else:
        nouveautes_affichees = nouveautes

    generer_rapport(etat, nouveautes_affichees, rapports, criteres, aujourdhui, Path(args.sortie), scoring)
    n_match = sum(1 for a in nouveautes if a["classement"] == "match")
    log(f"\nRapport écrit : {args.sortie}")
    log(f"{len(nouveautes)} nouveauté(s) dont {n_match} correspondant aux critères.")


if __name__ == "__main__":
    main()
