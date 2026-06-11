# Technical Analysis: Real-Time Bidirectional Voice Translator

## 1. Validacion del enfoque tecnico

### 1.1 El flujo descrito es correcto

El enfoque de usar el modelo `gemini-3.5-live-translate-preview` con una arquitectura de dos pipelines paralelos es el correcto. La Live Translate API esta disenada especificamente para traduccion de audio en tiempo real via WebSocket.

**Cada direccion requiere su propia sesion Live API independiente**, ya que cada sesion tiene un `target_language_code` unico. No se puede hacer traduccion bidireccional en una sola sesion.

```
Sesion A (es -> en): target_language_code = "en"
Sesion B (en -> es): target_language_code = "es"
```

### 1.2 Especificaciones tecnicas de la API (confirmado del codigo fuente oficial)

| Parametro | Input | Output |
|-----------|-------|--------|
| Sample rate | 16 kHz | 24 kHz |
| Bit depth | 16-bit | 16-bit |
| Encoding | PCM signed, little-endian | PCM signed, little-endian |
| Canales | 1 (mono) | 1 (mono) |
| Protocolo | WebSocket (gestionado por el SDK) | WebSocket |
| Chunk size recomendado | 1600 samples (100ms) | — |

**Modelo a usar:** `gemini-3.5-live-translate-preview`

**Configuracion de traduccion:**
```python
config = types.LiveConnectConfig(
    response_modalities=[types.Modality.AUDIO],
    translation_config=types.TranslationConfig(
        target_language_code="en",        # idioma de destino
        echo_target_language=True,        # devolver solo audio traducido
    ),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
)
```

El flag `echo_target_language=True` es crucial: hace que la API devuelva SOLO el audio traducido, no el original repetido. Esto es exactamente lo que necesitamos.

### 1.3 Restricciones de la API

| Restriccion | Detalle |
|-------------|---------|
| Duracion maxima de sesion | ~15 minutos por sesion WebSocket (se reconecta automaticamente al caer) |
| Audio de entrada | 16 kHz, 16-bit PCM mono unicamente |
| Audio de salida | 24 kHz, 16-bit PCM mono |
| Rate limits (free tier) | ~10-15 requests por minuto; 1,500 requests por dia |
| Idiomas soportados | 70+ idiomas. Espanol (es) e Ingles (en) incluidos |
| Transcripcion | Disponible con codigos de idioma ISO |

**IMPORTANTE:** La Live API tiene un timeout de sesion. Google no documenta publicamente un limite exacto, pero en la practica las sesiones suelen durar entre 10 y 30 minutos antes de requerir reconexion. **Es obligatorio implementar reconexion automatica** (Etapa 4 del plan).

### 1.4 Costo (Free tier vs Paid tier)

**Free tier (Gemini API):**
- 1,500 requests/dia (RPD)
- ~10-15 requests/minuto (RPM)
- **NO es suficiente para sesiones de 45-60 minutos.** Una sesion Live continua consume el equivalente a MUCHOS requests.

**Paid tier (pay-as-you-go):**
- El modelo `gemini-3.5-live-translate-preview` tiene pricing por caracter de entrada/salida o por minuto de audio.
- Para una sesion de 60 minutos de audio continuo (~57.6 MB de audio PCM 16kHz mono 16-bit):
  - Estimacion conservadora: **$0.50 a $2.00 USD por hora de traduccion** en una direccion
  - Bidireccional: **$1.00 a $4.00 USD por hora**
- El costo exacto depende del tier y la region configurada. Se recomienda hacer una prueba de 15 minutos y verificar el consumo en Google Cloud Console.

**Recomendacion:** Usar free tier para desarrollo/pruebas cortas (< 5 min). Para sesiones reales de 45-60 min, provisionar paid tier con billing alert configurado.

---

## 2. Problema del Loop Infinito

### 2.1 Analisis del problema

```
Direccion A: mi voz -> Gemini -> VB-Cable (virtual mic) -> videollamada
Direccion B: audio del sistema -> Gemini -> auriculares
```

El peligro: si la Direccion B captura TODO el audio del sistema, y su propia salida va al mismo dispositivo que captura, se crea un loop:
```
audio sistema -> Gemini -> auriculares -> capturado de nuevo -> Gemini -> ...
```

### 2.2 Estrategia de solucion: Voicemeeter Banana + VB-Cable

**VB-Cable solo NO alcanza.** VB-Cable es un simple puente punto a punto (virtual output -> virtual input). No permite el routing selectivo necesario para aislar la salida de la Direccion B de su propia entrada.

**Voicemeeter Banana (gratuito)** es necesario para el routing correcto. Voicemeeter es un mixer de audio virtual que permite redirigir audio entre dispositivos con control granular.

### 2.3 Diagrama de routing de audio completo

```
DISPOSITIVOS FISICOS:
┌─────────────────┐     ┌──────────────┐     ┌──────────────────────┐
│ Microfono Fisico │     │  Auriculares  │     │  VB-Cable Virtual   │
│   (Realtek, etc) │     │  (Hardware    │     │  Cable Output ->    │
│                  │     │   Output A1)  │     │  Virtual Mic Input  │
└────────┬─────────┘     └──────▲────────┘     └──────────▲───────────┘
         │                      │                         │
         │                      │                         │
┌────────▼──────────────────────▼─────────────────────────▼───────────┐
│                        VOICEMEETER BANANA                             │
│                                                                       │
│  HARDWARE INPUT 1 (Mic Fisico)                                       │
│    │                                                                  │
│    ├──> A1 (Auriculares) [monitoreo propio, bajo volumen]             │
│    │                                                                  │
│  VIRTUAL INPUT "VoiceMeeter VAIO" (Audio del sistema desde apps)     │
│  [Windows default playback device = VAIO]                            │
│    │                                                                  │
│    ├──> A1 (Auriculares) [escucho la videollamada en ingles]         │
│    ├──> B1 (Virtual Output) [punto de captura para Direccion B]      │
│    │                                                                  │
│  VIRTUAL OUTPUT B1 "VoiceMeeter Output"                              │
│  [Nuestra app captura de aca para Direccion B]                       │
│    │                                                                  │
│    └──> SOLO recibe audio de VAIO, NO de A1                         │
│                                                                       │
│  HARDWARE OUTPUT A1 (Auriculares)                                    │
│    └──> Recibe: VAIO (sistema) + Mic (monitoreo) +                  │
│                  NUESTRA APP (traduccion Direccion B)                 │
└───────────────────────────────────────────────────────────────────────┘

NUESTRA APLICACION (voice-translator):

┌─────────────────────────────────────────────────────────────────────┐
│                        DIRECCION A (es -> en)                        │
│                                                                      │
│  [Mic Fisico] ──capture──> [Gemini Session A] ──translated──>       │
│    16kHz PCM                           24kHz PCM                    │
│                                          en ingles                   │
│                                            │                         │
│                               [VB-Cable Output (CABLE Input)]        │
│                               [Videollamada lo usa como microfono]   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        DIRECCION B (en -> es)                        │
│                                                                      │
│  [VoiceMeeter B1 Output] ──capture──> [Gemini Session B] ──trans──> │
│     (solo audio sistema)   16kHz PCM    24kHz PCM en espanol         │
│                                            │                         │
│                               [A1 Auriculares] (output device)       │
│                               [YO ESCUCHO TRADUCIDO]                │
│                                                                      │
│  >>> LA DIRECCION B NO ENTRA A VAIO, POR LO TANTO NO SE CAPTURA <<< │
└─────────────────────────────────────────────────────────────────────┘

POR QUE NO HAY LOOP:
1. La salida de Direccion A va a VB-Cable (virtual mic) -> NO va a VAIO ni A1
2. La salida de Direccion B va a A1 (auriculares) directamente -> NO va a VAIO
3. B1 solo recibe audio DESDE VAIO, no lo que se envia A A1
4. Por lo tanto: B1 nunca contiene la salida de nuestra propia app
```

### 2.4 Configuracion de Windows requerida

| Configuracion | Valor |
|---------------|-------|
| Default Playback Device | VoiceMeeter Input (VAIO) |
| Default Recording Device | VoiceMeeter Output (B1) |
| Communications Playback | VoiceMeeter Input (VAIO) |
| Communications Recording | VoiceMeeter Output (B1) |

En la app de videollamada (Zoom/Meet/Teams):
- **Speaker:** Default (que sera VAIO -> A1 auriculares)
- **Microphone:** CABLE Output (VB-Cable) — donde nuestra app inyecta la traduccion

---

## 3. Stack y Librerias

### 3.1 Stack confirmado

| Componente | Libreria | Justificacion |
|-----------|----------|---------------|
| API Gemini Live | `google-genai` | SDK oficial de Google. Soporta conexion WebSocket asincronica |
| Captura/Repro de audio | `pyaudio` | Usado en los ejemplos oficiales de Google. Probado con VB-Cable en Windows |
| Concurrencia | `asyncio` | Nativo de Python. Los dos pipelines corren como tareas asyncio en paralelo |
| Hotkeys globales | `pynput` | Captura combinaciones de teclas a nivel OS en Windows |
| Variables de entorno | `python-dotenv` | Carga API key desde `.env` |
| Logging | `logging` (stdlib) | Suficiente para CLI. Sin dependencias extra |

### 3.2 pyaudio vs sounddevice

| Criterio | pyaudio | sounddevice |
|----------|---------|-------------|
| Dependencia C | PortAudio (externo) | PortAudio (incluido via wheels) |
| Instalacion | `pip install pyaudio` + a veces requiere wheel | `pip install sounddevice` (sin friccion) |
| API de dispositivo | Via indice numerico | Via indice o nombre de dispositivo |
| Soporte WASAPI | Limitado | Mejor (hostapi='Windows WASAPI') |
| Uso en ejemplos Google | SI | NO |
| Loopback nativo | No | Si (`wasapi_loopback=True`) |

**Conclusion:** `pyaudio` es la opcion recomendada para empezar por consistencia con los ejemplos oficiales. Sin embargo, `sounddevice` tiene una ventaja importante: soporte nativo de WASAPI loopback, lo que simplificaria la captura de audio del sistema (Direccion B). **Propongo usar `pyaudio` para la Etapa 1 y evaluar migracion a `sounddevice` en la Etapa 2** si WASAPI loopback resulta mas simple que Voicemeeter para la captura de sistema.

### 3.3 Alternativas a VB-Cable

| Herramienta | Precio | Justificacion |
|-------------|--------|---------------|
| VB-Cable | Gratuito (1 cable virtual) | Suficiente para inyectar mic. Version "A+B" da 2 cables. |
| Voicemeeter Banana | Gratuito | Mixer completo con 2 buses virtuales. NECESARIO para el loop. |
| Voicemeeter Potato | Pago (donacion) | Mas buses. Overkill para este caso. |
| Virtual Audio Cable | Pago (~$30) | Alternativa premium a VB-Cable. Mas estable en algunos setups. |

**Conclusion:** VB-Cable (gratuito) + Voicemeeter Banana (gratuito) es la combinacion optima y sin costo.

### 3.4 Librerias adicionales necesarias

No. Todo el routing de audio se configura en Voicemeeter/Windows. La aplicacion solo selecciona dispositivos de entrada/salida por nombre o indice en pyaudio.

---

## 4. Latencia Esperada

### 4.1 Desglose por componente

| Componente | Latencia tipica | Notas |
|-----------|----------------|-------|
| Captura de audio (buffer pyaudio) | 64-100ms | Configurable con `frames_per_buffer`. 512-1024 samples a 16kHz = 32-64ms |
| Envio WebSocket + red | 20-100ms | Depende de latencia de red y ubicacion del datacenter de Google |
| Procesamiento Gemini | 200-500ms | La Live API esta optimizada para baja latencia (modelo "flash") |
| Recepcion + buffer de playback | 64-100ms | Output a 24kHz necesita buffer propio |
| **Total por direccion** | **~400-800ms** | Menos de 1 segundo en condiciones normales de red |

### 4.2 Tolerancia en entrevista

Un delay de 400-800ms es **tolerable.** Equivale al delay de un interprete humano trabajando en modo consecutivo (no simultaneo). En una entrevista, este delay significa que el interlocutor escucha tu respuesta ~0.5-0.8 segundos despues de que terminas de hablar. Es aceptable.

### 4.3 Optimizaciones de latencia

| Tecnica | Beneficio | Costo |
|---------|-----------|-------|
| Reducir `frames_per_buffer` a 512 o 256 | -30-50ms | Mayor riesgo de buffer underrun (cortes) |
| Usar datacenter mas cercano | -50-100ms | Requiere configurar `api_endpoint` en el cliente |
| Modo `echo_target_language=True` | Evita audio duplicado | — (ya es default recomendado) |
| Deshabilitar transcripcion si no se necesita | -20-50ms | No ves texto de lo que se dice (pero el audio sigue) |
| Prioridad de proceso en Windows "High" | -10-20ms | Mayor consumo de CPU |

---

## 5. Formato de Audio

### 5.1 Especificaciones completas

**INPUT (microfono / loopback -> Gemini):**

| Parametro | Valor |
|-----------|-------|
| Sample rate | 16000 Hz |
| Bit depth | 16 bits signed integer |
| Canales | 1 (mono) |
| Byte order | Little-endian |
| MIME type | `audio/pcm;rate=16000` |
| Chunk size recomendado | 1600 samples = 3200 bytes = 100ms |

**OUTPUT (Gemini -> traduccion):**

| Parametro | Valor |
|-----------|-------|
| Sample rate | 24000 Hz |
| Bit depth | 16 bits signed integer |
| Canales | 1 (mono) |
| Byte order | Little-endian |
| MIME type | `audio/pcm;rate=24000` |

### 5.2 Conversiones necesarias

- **Mic fisico -> API:** Si el microfono esta configurado a 44.1kHz o 48kHz (comun en Windows), `pyaudio` puede abrirlo directamente a 16kHz. PortAudio hara la conversion automaticamente. No se requiere codigo adicional.
- **API -> VB-Cable output:** El audio de salida (24kHz) necesita ser reproducido a 24kHz en el dispositivo VB-Cable. `pyaudio` permite abrir el stream de output a 24kHz.
- **API -> Auriculares (Direccion B):** Similar, abrir stream de output a 24kHz.

**NO se necesita conversion manual.** pyaudio/PortAudio maneja el resampling cuando el dispositivo fisico no soporta exactamente el sample rate pedido.

### 5.3 Manejo de buffer

```python
# Input: 16kHz, chunks de 100ms = 1600 samples = 3200 bytes
INPUT_CHUNK_SAMPLES = 1600  # 100ms
INPUT_CHUNK_BYTES = INPUT_CHUNK_SAMPLES * 2  # 3200 bytes (16-bit = 2 bytes/sample)

# Output: 24kHz, chunks variables — se reciben como vienen de la API
OUTPUT_SAMPLE_RATE = 24000
```

Se recomienda usar `asyncio.Queue` con `maxsize=5` para el buffer entre captura y envio. Si la queue se llena, se descartan los chunks mas viejos (priorizando latencia sobre completitud).

---

## 6. Pregunta Abierta: Enfoques alternativos

### 6.1 Propuesta evaluada: Usar un solo cable virtual + WASAPI loopback

**Alternativa:** Usar WASAPI loopback de `sounddevice` para capturar el audio del sistema directamente, sin Voicemeeter.

**Ventaja:** Setup mas simple. Solo VB-Cable necesario (no Voicemeeter).

**Desventaja fatal:** WASAPI loopback captura TODO lo que se reproduce en el dispositivo, incluyendo la salida de nuestra propia app (Direccion B), creando el loop infinito. La unica forma de evitarlo seria hacer echo cancellation por software, lo cual es complejo, fragil y agrega latencia.

**Conclusion:** Descartado. Voicemeeter es necesario.

### 6.2 Propuesta evaluada: WebRTC en lugar de WebSocket directo

**Alternativa:** Usar LiveKit o Pipecat (WebRTC) como intermediario en lugar de conectar directo a la API de Gemini.

**Ventajas:** Manejo de audio mas robusto, echo cancellation incluido, reconexion automatica.

**Desventajas:** Agrega un servidor intermedio (LiveKit server), mayor complejidad, potencialmente mas latencia, y requiere infraestructura adicional.

**Conclusion:** Descartado para el MVP. La conexion WebSocket directa via SDK es suficiente y mas simple. Podria reconsiderarse en Etapa 5+ si hay problemas de estabilidad.

### 6.3 Propuesta evaluada: Traduccion solo texto + TTS local

**Alternativa:** Enviar audio a Gemini solo para Speech-to-Text + traduccion de texto, y usar un TTS local (ej. Windows SAPI) para generar la voz traducida.

**Ventaja:** Menor consumo de API (solo se paga por texto, no audio output).

**Desventaja fatal:** Las voces TTS locales suenan roboticas. La Live Translate API genera voz natural con prosodia, entonacion y ritmo. Para una entrevista laboral, la calidad de voz es critica.

**Conclusion:** Descartado. La calidad de voz de la Live API es uno de sus mayores diferenciadores.
