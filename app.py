# app.py
# -*- coding: utf-8 -*-
"""
Aplicación web de reservas que procesa mensajes de texto y audio.
Si faltan datos críticos (por ejemplo, destino o fecha de salida) se repregunta.
Se usa Flask con sesiones para mantener el estado de la conversación.
"""

import os
import re
import json
import datetime
import torch
import spacy
from langdetect import detect
from transformers import MarianMTModel, MarianTokenizer
import whisper
import random
from pymongo import MongoClient
from gtts import gTTS
from flask import Flask, render_template, request, jsonify, url_for, session, send_from_directory
from textblob import TextBlob

# Inicialización de Flask y configuración de la sesión
app = Flask(__name__)
app.secret_key = "mi_clave_secreta"  # Cambia esta clave por una segura en producción

# Directorios para archivos generados
if not os.path.exists("static"):
    os.makedirs("static")
if not os.path.exists("uploads"):
    os.makedirs("uploads")

# Cargar modelos de spaCy
nlp_en = spacy.load("en_core_web_sm")
nlp_es = spacy.load("es_core_news_sm")

# Cargar el modelo de traducción MarianMT (de español a inglés)
model_name = "Helsinki-NLP/opus-mt-es-en"
tokenizer = MarianTokenizer.from_pretrained(model_name)
model = MarianMTModel.from_pretrained(model_name)

# Conexión a MongoDB (ajusta la cadena de conexión según corresponda)
client = MongoClient("mongodb+srv://piereins:vAe99x0imoZryMl7@cluster0.qm0ja.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
db = client["DragonTravel"]
collectionReservas = db["reservas"]
collectionAeropuertos = db["aeropuertos"]
collectionAerolineas = db["aerolineas"]

# Se inicializa la reserva
ORIGINAL_DICT = {
        "client_name": "",
        "language": "",
        "origen": "",
        "destino": "",
        "round_trip": "",
        "fecha_ida": "",
        "fecha_regreso": "",
        "numero_pasajeros": "",
        "aerolinea": "",
        "bool_comentario": "",
        "comentario": "",
        "sentiment_analysis": ""
    }


# Funciones auxiliares

def translate_text(text):
    """Traduce texto al inglés usando MarianMT."""
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True)
    with torch.no_grad():
        translated_tokens = model.generate(**inputs)
    translated_text = tokenizer.batch_decode(translated_tokens, skip_special_tokens=True)
    return translated_text[0]

# Expresión regular para detectar el número de pasajeros
pattern_personas = re.compile(
    r"(?:(?P<number>\d+|one|two|three|four|five|six|seven|eight|nine|ten|a|an|uno|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+)?"
    r"(?:(?:round[-\s]?trip|one[-\s]?way(?:\s+trip)?)\s+)?"
    r"(?P<term>people|ticket(?:s)?|passengers|seats|pasajes|boletos|flight(?:s)?|vuelo(?:s)?|passages)",
    re.IGNORECASE
)

numeros_palabras = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1,
    "an": 1,
    "uno": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10
}

def es_aerolinea(entidad):
    keywords = ["air", "aero", "airlines", "airways", "fly", "flight", "iberia"]
    return any(kw in entidad.lower() for kw in keywords)

def obtener_idioma_preguntas(mensaje):
    mensaje_lower = mensaje.lower()
    tokens = set(mensaje_lower.split())
    spanish_words = {"quiero", "necesito", "boletos", "pasajes", "vuelos", "de", "para", "con", "y"}
    english_words = {"want", "need", "tickets", "flights", "from", "to", "with", "and"}
    tiene_es = bool(spanish_words.intersection(tokens))
    tiene_en = bool(english_words.intersection(tokens))
    if tiene_es and tiene_en:
        return "mixed"
    return "es" if tiene_es else "en"

# Prompts de repregunta (se pueden extender)
prompts_es = {
    "client_name": "Por favor, ingrese su nombre: ",
    "origen": "Por favor, ingrese el origen: ",
    "destino": "Por favor, ingrese el destino: ",
    "fecha_ida": "Por favor, ingrese la fecha de salida: ",
    "fecha_regreso": "Por favor, ingrese la fecha de regreso: ",
    "numero_pasajeros": "Por favor, ingrese el número de pasajeros: ",
    "round_trip": "¿Es su vuelo de ida y vuelta? (si/no): ",
    "bool_comentario": "¿Te gustaría dejarnos un comentario o sugerencia? (si/no):",
    "comentario": "Cuéntanos, ¿qué te pareció nuestro servicio? 😟"
}
prompts_en = {
    "client_name": "Please enter your name: ",
    "language": "Por favor, presiona 1 para inglés o 2 para español.<br>Please press 1 for English or 2 for Spanish.",
    "origen": "Please enter origin: ",
    "destino": "Please enter destination: ",
    "fecha_ida": "Please enter departure date: ",
    "fecha_regreso": "Please enter return date: ",
    "numero_pasajeros": "Please enter number of passengers: ",
    "round_trip": "Is your flight round trip? (yes/no): ",
    "bool_comentario": "Would you like to leave us a comment or suggestion?(yes/no):",
    "comentario": "Tell us, how did you find our service? 😟"
}

def generate_prompt_for_field(field, lang_mode="es"):
    if lang_mode == "en":
        return prompts_en.get(field, f"Please enter {field}: ")
    else:
        return prompts_es.get(field, f"Por favor, ingrese {field}: ")

def convert_date(date_str):
    """
    Convierte una cadena de fecha a formato "%d/%m/YYYY".
    Si el mes es anterior a marzo o (en marzo) el día es menor a 15, se asigna 2026; de lo contrario 2025.
    """
    if not date_str:
        return ""
    date_str = date_str.strip(" ,.")
    patterns = [
        re.compile(r"(?P<day>\d{1,2})\s+de\s+(?P<month>[A-Za-záéíóúñ]+)", re.IGNORECASE),
        re.compile(r"the\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+of\s+(?P<month>[A-Za-z]+)", re.IGNORECASE),
        re.compile(r"(?P<month>[A-Za-záéíóúñ]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?", re.IGNORECASE),
        re.compile(r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>[A-Za-záéíóúñ]+)", re.IGNORECASE),
    ]
    match = None
    for pattern in patterns:
        match = pattern.search(date_str)
        if match:
            break
    if match:
        try:
            day = int(match.group("day"))
            month_name = match.group("month").lower()
            months = {
                "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
                "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
                "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
                "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
            }
            if month_name in months:
                month = months[month_name]
                year = 2026 if (month < 3 or (month == 3 and day < 15)) else 2025
                date_obj = datetime.datetime(year=year, month=month, day=day)
                return date_obj.strftime("%d/%m/%Y")
            else:
                return date_str
        except Exception:
            return date_str
    return date_str

def procesar_mensaje(mensaje):
    lang_mode = obtener_idioma_preguntas(mensaje)
    mensaje_traducido = translate_text(mensaje)
    idioma_original = detect(mensaje)
    if idioma_original == "es":
        doc = nlp_es(mensaje)
    else:
        doc = nlp_en(mensaje_traducido)

    data = {
        "client_name": "",
        "origen": "",
        "destino": "",
        "round_trip": "",
        "fecha_ida": "",
        "fecha_regreso": "",
        "numero_pasajeros": "",
        "aerolinea": "",
        "bool_comentario": "",
        "comentario": "",
        "sentiment_analysis": ""
    }
    exclusion = {"i want", "buy", "reserve", "need", "quiero", "necesito"}
    # Extracción de lugares
    lugares = [
        ent.text for ent in doc.ents
        if ent.label_ in ("GPE", "LOC") and not es_aerolinea(ent.text) and ent.text.lower() not in exclusion
    ]
    if len(lugares) < 2:
        pattern_od = re.compile(r"de\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+)\s+a\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+)", re.IGNORECASE)
        match_od = pattern_od.search(mensaje)
        if match_od:
            lugares = [match_od.group(1).strip(), match_od.group(2).strip()]
    if len(lugares) >= 2:
        data["origen"] = lugares[0]
        data["destino"] = lugares[1]
    elif len(lugares) == 1:
        data["origen"] = lugares[0]

    if data["destino"]:
        if " for " in data["destino"]:
            data["destino"] = data["destino"].split(" for ")[0].strip()
        data["destino"] = re.sub(r'\s+el$', '', data["destino"].strip(), flags=re.IGNORECASE)

    # Extracción de fechas
    fechas = [ent.text for ent in doc.ents if ent.label_ == "DATE"]
    if idioma_original == "es" and len(fechas) < 1:
        pattern_fecha_es = re.compile(
            r"(?:el\s+)?(\d{1,2}\s*de\s*(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre))",
            re.IGNORECASE
        )
        fechas_fallback = pattern_fecha_es.findall(mensaje)
        for fecha in fechas_fallback:
            if fecha not in fechas:
                fechas.append(fecha)
    if idioma_original != "es" and len(fechas) < 1:
        pattern_date_en = re.compile(r"back on\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?)", re.IGNORECASE)
        match_date_en = pattern_date_en.search(mensaje_traducido)
        if match_date_en:
            fechas.append(match_date_en.group(1).strip())
    if fechas:
        data["fecha_ida"] = fechas[0]
    # Número de pasajeros
    match_personas = pattern_personas.search(mensaje_traducido)
    if match_personas:
        num_str = match_personas.group("number")
        if num_str is None:
            num = 1
        else:
            num = numeros_palabras.get(num_str.lower(), int(num_str) if num_str.isdigit() else 1)
        data["numero_pasajeros"] = num

    # Buscar aerolínea
    airline_detected = ""
    for ent in doc.ents:
        if ent.label_ in ("ORG", "GPE", "LOC") and es_aerolinea(ent.text):
            airline_detected = ent.text
            break
    data["aerolinea"] = airline_detected

    return data

def check_missing_fields(reserva):
    """Verifica si faltan campos críticos; por ejemplo: destino y fecha de salida."""
    missing = []
    if not reserva.get("language"):
        missing.append("language")
    if not reserva.get("origen"):
        missing.append("origen")
    if not reserva.get("round_trip"):
        missing.append("round_trip")
    if not reserva.get("destino"):
        missing.append("destino")
    if not reserva.get("fecha_ida"):
        missing.append("fecha_ida")
    if not reserva.get("fecha_regreso"):
        missing.append("fecha_regreso")
    if not reserva.get("numero_pasajeros"):
        missing.append("numero_pasajeros")
    if not reserva.get("bool_comentario"):
        missing.append("bool_comentario")
    return missing

def finalizar_reserva(reserva, language):
    """Realiza los ajustes finales en la reserva, actualiza datos de aeropuertos y aerolíneas,
    genera el resumen, crea el audio y la inserción en MongoDB."""
    airlines = ["KLM", "Delta", "American Airlines", "British Airways", "Lufthansa", "Air France", 
                "Emirates", "Qatar Airways", "Singapore Airlines", "Cathay Pacific"]
    reserva["origen"] = buscar_aeropuerto(reserva.get("origen"), collectionAeropuertos) + " en " + reserva.get("origen", "")
    reserva["destino"] = buscar_aeropuerto(reserva.get("destino"), collectionAeropuertos) + " en " + reserva.get("destino", "")
    if not reserva.get("aerolinea"):
        reserva["aerolinea"] = random.choice(airlines)
    reserva["aerolinea"] = buscar_aerolinea(reserva.get("aerolinea"), collectionAerolineas)

    if reserva.get("bool_comentario") and reserva.get("comentario"):
        comentario = reserva.get("comentario")
        blob = TextBlob(comentario)
        sentiment = blob.sentiment
        # Guarda la polaridad y la subjetividad
        reserva["sentiment_analysis"] = {
            "polarity": sentiment.polarity,
            "subjectivity": sentiment.subjectivity
        }

    inserted_id = insertar_reserva(reserva, collectionReservas)
    print("ID del documento insertado:", inserted_id)
    resumen = generar_resumen_reserva(reserva, language)
    audio_output_path = os.path.join("static", "audio.mp3")
    tts = gTTS(text=resumen, lang=language)
    tts.save(audio_output_path)
    return resumen, url_for("static", filename="audio.mp3")

def generar_resumen_reserva(reserva, language):
    """Genera el mensaje resumen de la reserva."""
    if language == "es":
        if reserva.get('round_trip'):
            texto_tipo = "ida y vuelta"
            resumen = (
                f"Detalles de su reserva:\n"
                f"Estimado {reserva.get('client_name')},\n"
                f"su vuelo tiene como Origen {reserva.get('origen').title()}.\n"
                f"El destino es {reserva.get('destino').title()}.\n"
                f"Su tipo de viaje es {texto_tipo}.\n"
                f"La fecha de ida es {reserva.get('fecha_ida')}.\n"
                f"La fecha de regreso es {reserva.get('fecha_regreso')}.\n"
                f"El número de personas es {reserva.get('numero_pasajeros')}.\n"
                f"Viajará en la Aerolínea {reserva.get('aerolinea')}.\n"
                f"Gracias por elegirnos."
            )
        else:
            texto_tipo = "solo ida"
            resumen = (
                f"Detalles de su reserva:\n"
                f"Estimado {reserva.get('client_name')},\n"
                f"su vuelo tiene como Origen {reserva.get('origen').title()}.\n"
                f"El destino es {reserva.get('destino').title()}.\n"
                f"Su tipo de viaje es {texto_tipo}.\n"
                f"La fecha de ida es {reserva.get('fecha_ida')}.\n"
                f"El número de personas es {reserva.get('numero_pasajeros')}.\n"
                f"Viajará en la Aerolínea {reserva.get('aerolinea')}.\n"
                f"Gracias por elegirnos."
            )
    elif language == "en":
        if reserva.get('round_trip'):
            trip_type = "round trip"
            summary = (
                f"Booking details:\n"
                f"Dear {reserva.get('client_name')},\n"
                f"your flight has {reserva.get('origen').title()} as Origin.\n"
                f"The destination is {reserva.get('destino').title()}.\n"
                f"Your trip type is {trip_type}.\n"
                f"The departure date is {reserva.get('fecha_ida')}.\n"
                f"The return date is {reserva.get('fecha_regreso')}.\n"
                f"The number of passengers is {reserva.get('numero_pasajeros')}.\n"
                f"You will travel with {reserva.get('aerolinea')}.\n"
                f"Thank you for choosing us."
            )
        else:
            trip_type = "one way"
            summary = (
                f"Booking details:\n"
                f"Dear {reserva.get('client_name')},\n"
                f"your flight has {reserva.get('origen').title()} as Origin.\n"
                f"The destination is {reserva.get('destino').title()}.\n"
                f"Your trip type is {trip_type}.\n"
                f"The departure date is {reserva.get('fecha_ida')}.\n"
                f"The number of passengers is {reserva.get('numero_pasajeros')}.\n"
                f"You will travel with {reserva.get('aerolinea')}.\n"
                f"Thank you for choosing us."
            )
        resumen = summary
    else:
        resumen = "Language not supported."

    return resumen

def buscar_aeropuerto(origen, coleccion):
    """Busca el aeropuerto según el origen."""
    documento = coleccion.find_one({"city": re.compile(f"^{origen}", re.IGNORECASE)})
    if documento:
        return documento.get("name")
    documento = coleccion.find_one({"state": re.compile(f"^{origen}", re.IGNORECASE)})
    if documento:
        return documento.get("name")
    return "Aeropuerto de " + origen

def buscar_aerolinea(aerolinea, coleccion):
    """Busca la aerolínea."""
    documento = coleccion.find_one({"Callsign": re.compile(f"^{aerolinea}", re.IGNORECASE)})
    if documento:
        return documento.get("Name")
    documento = coleccion.find_one({"Name": re.compile(f"^{aerolinea}", re.IGNORECASE)})
    if documento:
        return documento.get("Name")
    return "Aerolinea local"

def insertar_reserva(reserva, collection):
    """Inserta la reserva en MongoDB."""
    resultado = collection.insert_one(reserva)
    print(reserva)
    return resultado.inserted_id

# Manejo de la conversación en estado (para mensajes de texto)

from flask import Flask, request, jsonify, session

app = Flask(__name__)
app.secret_key = "your_secret_key"  # Required for session handling

from flask import Flask, request, jsonify, session

app = Flask(__name__)
app.secret_key = "your_secret_key"  # Required for session handling

@app.route("/process_message", methods=["POST"])
def process_message():
    mensaje = request.form.get("message", "").strip()
    if not mensaje:
        return jsonify({"response": "No se recibió ningún mensaje."})

    lang_mode = obtener_idioma_preguntas(mensaje)

    # Check if we are in the middle of a conversation (handling pending fields)
    if "pending_fields" in session and session["pending_fields"]:
        print(session.get("reserva", {}))
        field = session["pending_fields"][0]
        reserva = session.get("reserva", {})

        pending = session["pending_fields"]

        # Ensure client_name is not lost
        client_name = reserva.get("client_name", "Guest")

        # Language selection handling
        if field == "language":
            if mensaje == "1":
                reserva[field] = 'en'
            elif mensaje == "2":
                reserva[field] = 'es'
            else:
                prompt = generate_prompt_for_field('language', lang_mode)
                return jsonify({"response": prompt})

            pending.pop(0)
            reserva["client_name"] = client_name  # Re-store client_name before saving
            session["reserva"] = reserva
            session["pending_fields"] = pending
            session.modified = True  # Ensure session changes persist

            if reserva[field] == "en":
                return jsonify({"response": f"Hi, {client_name}. Tell us, how can we help you?"})
            else:
                return jsonify({"response": f"Hola, {client_name}. Cuéntanos, ¿cómo podemos ayudarte?"})

        # Origin handling
        if field == "origen":
            reserva.update(procesar_mensaje(mensaje.lower()))  # Update instead of overwrite
        elif field == "fecha_ida" or field == "fecha_regreso":
            reserva[field] = convert_date(mensaje)
        elif field == "round_trip":
            # Handle round_trip field
            if mensaje.lower() in ["sí", "si", "yes", "y"]:
                reserva[field] = True
            else:
                reserva[field] = False

            # Adjust pending_fields based on round_trip value
            if reserva[field]:  # If round_trip is True, add fecha_regreso to pending_fields
                if "fecha_regreso" not in pending:
                    pending.append("fecha_regreso")
            else:  # If round_trip is False, remove fecha_regreso from pending_fields if it exists
                if "fecha_regreso" in pending:
                    pending.remove("fecha_regreso")
        elif field == "bool_comentario":
            if mensaje.lower() in ["sí", "si", "yes", "y"]:
                reserva[field] = True
            else:
                reserva[field] = False

            if reserva[field]:  # If bool_comentario is True, add comentario
                if "comentario" not in pending:
                    pending.append("comentario")
            else:  # If round_trip is False, remove fecha_regreso from pending_fields if it exists
                if "comentario" in pending:
                    pending.remove("comentario")
        else:
            reserva[field] = mensaje

        pending.pop(0)
        reserva["client_name"] = client_name  # Ensure client_name persists
        session["reserva"] = reserva
        session["pending_fields"] = pending
        session.modified = True

        if pending:
            prompt = generate_prompt_for_field(pending[0], reserva.get("language", "en"))
            return jsonify({"response": prompt})
        else:
            resumen, audio_url = finalizar_reserva(reserva, reserva.get("language", "en")) 
            session.pop("reserva", None)
            gif_url = os.path.join("static", "plane.gif")  # URL to the GIF
            return jsonify({"response": resumen, "audio": audio_url, "gif": gif_url})

    else:
        # First interaction: start reservation process
        reserva = ORIGINAL_DICT.copy()
        reserva["client_name"] = mensaje  # Store user's name
        missing = check_missing_fields(reserva)

        session["reserva"] = reserva
        session["pending_fields"] = missing
        session.modified = True

        if missing:
            prompt = generate_prompt_for_field(missing[0], lang_mode)
            return jsonify({"response": prompt})
        else:
            resumen, audio_url = finalizar_reserva(reserva, reserva.get("language", "en"))
            session.pop("reserva", None)
            session.pop("pending_fields", None)
            # Serve the GIF file
            gif_url = os.path.join("static", "plane.gif")   # URL to the GIF
            return jsonify({"response": resumen, "audio": audio_url, "gif": gif_url})
            #return jsonify({"response": resumen, "audio": audio_url})

# Manejo de la conversación en estado para audio (proceso similar)

@app.route("/process_audio_message", methods=["POST"])
def process_audio_message():
    if "audio_data" not in request.files:
        return jsonify({"response": "No se recibió ningún archivo de audio."})
    audio_file = request.files["audio_data"]
    audio_path = os.path.join("uploads", "temp_audio.wav")
    audio_file.save(audio_path)
    mensaje = transcribe_audio(audio_path)
    print("Mensaje transcripto:", mensaje)
    lang_mode = obtener_idioma_preguntas(mensaje)
    
    # Check if we are in the middle of a conversation (handling pending fields)
    if "pending_fields" in session and session["pending_fields"]:
        print(session.get("reserva", {}))
        field = session["pending_fields"][0]
        reserva = session.get("reserva", {})

        pending = session["pending_fields"]

        # Ensure client_name is not lost
        client_name = reserva.get("client_name", "Guest")

        # Language selection handling
        if field == "language":
            if mensaje == "1":
                reserva[field] = 'en'
            elif mensaje == "2":
                reserva[field] = 'es'
            else:
                prompt = generate_prompt_for_field('language', lang_mode)
                return jsonify({"response": prompt})

            pending.pop(0)
            reserva["client_name"] = client_name  # Re-store client_name before saving
            session["reserva"] = reserva
            session["pending_fields"] = pending
            session.modified = True  # Ensure session changes persist

            if reserva[field] == "en":
                return jsonify({"response": f"Hi, {client_name}. Tell us, how can we help you?"})
            else:
                return jsonify({"response": f"Hola, {client_name}. Cuéntanos, ¿cómo podemos ayudarte?"})

        # Origin handling
        if field == "origen":
            reserva.update(procesar_mensaje(mensaje.lower()))  # Update instead of overwrite
        elif field == "fecha_ida" or field == "fecha_regreso":
            reserva[field] = convert_date(mensaje)
        elif field == "round_trip":
            # Handle round_trip field
            if mensaje.lower() in ["sí", "si", "yes", "y"]:
                reserva[field] = True
            else:
                reserva[field] = False

            # Adjust pending_fields based on round_trip value
            if reserva[field]:  # If round_trip is True, add fecha_regreso to pending_fields
                if "fecha_regreso" not in pending:
                    pending.append("fecha_regreso")
            else:  # If round_trip is False, remove fecha_regreso from pending_fields if it exists
                if "fecha_regreso" in pending:
                    pending.remove("fecha_regreso")
        elif field == "bool_comentario":
            if mensaje.lower() in ["sí", "si", "yes", "y"]:
                reserva[field] = True
            else:
                reserva[field] = False

            if reserva[field]:  # If bool_comentario is True, add comentario
                if "comentario" not in pending:
                    pending.append("comentario")
            else:  # If round_trip is False, remove fecha_regreso from pending_fields if it exists
                if "comentario" in pending:
                    pending.remove("comentario")
        else:
            reserva[field] = mensaje

        pending.pop(0)
        reserva["client_name"] = client_name  # Ensure client_name persists
        session["reserva"] = reserva
        session["pending_fields"] = pending
        session.modified = True

        if pending:
            prompt = generate_prompt_for_field(pending[0], reserva.get("language", "en"))
            return jsonify({"response": prompt})
        else:
            resumen, audio_url = finalizar_reserva(reserva, reserva.get("language", "en")) 
            session.pop("reserva", None)
            gif_url = os.path.join("static", "plane.gif")  # URL to the GIF
            return jsonify({"response": resumen, "audio": audio_url, "gif": gif_url})

    else:
        # First interaction: start reservation process
        reserva = ORIGINAL_DICT.copy()
        reserva["client_name"] = mensaje  # Store user's name
        missing = check_missing_fields(reserva)

        session["reserva"] = reserva
        session["pending_fields"] = missing
        session.modified = True

        if missing:
            prompt = generate_prompt_for_field(missing[0], lang_mode)
            return jsonify({"response": prompt})
        else:
            resumen, audio_url = finalizar_reserva(reserva, reserva.get("language", "en"))
            session.pop("reserva", None)
            session.pop("pending_fields", None)
            # Serve the GIF file
            gif_url = os.path.join("static", "plane.gif")   # URL to the GIF
            return jsonify({"response": resumen, "audio": audio_url, "gif": gif_url})

def transcribe_audio(audio_path):
    """Transcribe el audio usando Whisper."""
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    whisper_model = whisper.load_model("large", device=device)
    result = whisper_model.transcribe(audio_path)
    return result['text']

@app.route("/")
def index():
    return render_template("chat.html")

if __name__ == "__main__":
    app.run(debug=True)
