# Development Plan: Real-Time Bidirectional Voice Translator

## Overview

Cada etapa produce un entregable funcional y testeable por separado. No se avanza a la siguiente etapa sin verificar que la anterior funciona.

---

## Etapa 1: Pipeline Direccion A — Mi voz en espanol al virtual mic en ingles

### Objetivo
Que el usuario hable en espanol por su microfono fisico y el audio traducido al ingles aparezca en el dispositivo VB-Cable Output (que la app de videollamada usara como microfono).

### Prerrequisitos
- [x] Python 3.10+ instalado
- [x] VB-Cable instalado (aparece como "CABLE Input" en playback y "CABLE Output" en recording)
- [x] API key de Gemini configurada en variable de entorno `GEMINI_API_KEY`
- [x] `pip install -r requirements.txt`

### Tareas

| # | Tarea | Archivo | Estimacion |
|---|-------|---------|------------|
| 1.1 | Definir configuracion: constantes de audio, modelo, dispositivos | `main.py` | 15 min |
| 1.2 | Implementar `AudioCapture`: clase que captura del microfono fisico en 16kHz mono y pone en `asyncio.Queue` | `main.py` | 30 min |
| 1.3 | Implementar `GeminiSession`: clase wrapper para una sesion Live Translate API con `target_language_code="en"` | `main.py` | 45 min |
| 1.4 | Implementar `AudioPlayer`: clase que reproduce audio a VB-Cable Output en 24kHz mono | `main.py` | 20 min |
| 1.5 | Integrar el pipeline en `run_direction_a()`: captura -> Gemini -> VB-Cable | `main.py` | 30 min |
| 1.6 | Agregar logueo de transcripciones de entrada/salida para verificacion | `main.py` | 15 min |

### Verificacion
1. Ejecutar `python main.py --direction A`
2. Hablar en espanol al microfono
3. Abrir Windows Sound Settings > Recording > CABLE Output > Properties > Listen > "Listen to this device"
4. Escuchar el audio traducido al ingles por los auriculares

### Criterio de aceptacion
- [ ] El script arranca sin errores
- [ ] Se conecta a Gemini Live API exitosamente
- [ ] Al hablar en espanol, se escucha la traduccion en ingles en VB-Cable Output
- [ ] Las transcripciones de entrada y salida se imprimen en consola
- [ ] El script se detiene limpiamente con Ctrl+C

---

## Etapa 2: Pipeline Direccion B — Audio del sistema traducido a auriculares

### Objetivo
Capturar el audio del sistema (lo que reproduce la videollamada) usando Voicemeeter, traducirlo al espanol, y reproducirlo en los auriculares. Resolver definitivamente el loop infinito.

### Prerrequisitos
- [x] Etapa 1 completada
- [x] Voicemeeter Banana instalado y configurado segun el diagrama de routing del documento de analisis
- [x] Windows default playback = VoiceMeeter Input (VAIO)
- [x] Windows default recording = VoiceMeeter Output (B1)

### Tareas

| # | Tarea | Estimacion |
|---|-------|------------|
| 2.1 | Configurar Voicemeeter Banana: VAIO -> A1 + B1, mic solo -> A1 (sin B1) | 30 min |
| 2.2 | Agregar `LOOPBACK_DEVICE_NAME` a la configuracion (VoiceMeeter B1 Output) | 5 min |
| 2.3 | Modificar `AudioCapture` para soportar seleccion de dispositivo por nombre, no solo por indice | 30 min |
| 2.4 | Crear `run_direction_b()`: captura de B1 -> Gemini (target="es") -> auriculares | 30 min |
| 2.5 | Ejecutar ambos pipelines en paralelo con `asyncio.gather()` | 45 min |
| 2.6 | Probar que NO hay loop infinito: reproducir un video de YouTube en ingles mientras se habla en espanol | 30 min |

### Verificacion
1. Configurar Voicemeeter Banana como se indica
2. Abrir un video de YouTube en ingles (el audio sale por VAIO -> A1)
3. Ejecutar `python main.py`
4. Hablar en espanol por el microfono
5. Escuchar:
   - Por VB-Cable Output: tu voz traducida al ingles
   - Por auriculares (A1): el audio del video de YouTube traducido al espanol
6. Verificar que el audio del video y tu propia traduccion no generan eco ni loop

### Criterio de aceptacion
- [ ] Ambos pipelines corren simultaneamente sin bloquearse
- [ ] El audio del sistema (YouTube, videollamada) se captura correctamente desde B1
- [ ] La traduccion al espanol se reproduce en auriculares
- [ ] NO hay eco, feedback, ni loop infinito
- [ ] Ctrl+C detiene ambos pipelines limpiamente

---

## Etapa 3: Control Basico con Hotkeys

### Objetivo
Agregar control por teclado global: iniciar, pausar/reanudar, y detener la traduccion sin tocar el mouse. Feedback visual en consola.

### Tareas

| # | Tarea | Estimacion |
|---|-------|------------|
| 3.1 | Agregar `pynput` y crear listener global de teclado en un thread separado | 30 min |
| 3.2 | Definir hotkeys: `Ctrl+Shift+T` = toggle start/stop, `Ctrl+Shift+P` = toggle pause | 15 min |
| 3.3 | Implementar maquina de estados: IDLE -> RUNNING -> PAUSED -> RUNNING -> STOPPED | 30 min |
| 3.4 | Agregar feedback visual en consola con `\r` y colores: estado actual, tiempo de sesion | 30 min |
| 3.5 | Implementar pausa (detener captura/envio sin cerrar sesiones) | 30 min |
| 3.6 | Manejar transiciones de estado correctamente (no iniciar si ya esta corriendo, etc.) | 15 min |

### Verificacion
1. Ejecutar `python main.py`. Estado inicial: IDLE.
2. Presionar `Ctrl+Shift+T`. Debe mostrar: RUNNING. Empezar a traducir.
3. Presionar `Ctrl+Shift+P`. Debe mostrar: PAUSED. La traduccion se detiene.
4. Presionar `Ctrl+Shift+P` de nuevo. Debe mostrar: RUNNING. La traduccion se reanuda.
5. Presionar `Ctrl+Shift+T`. Debe mostrar: STOPPED. Sesiones cerradas.

### Criterio de aceptacion
- [ ] Hotkeys globales funcionan incluso con otra ventana en foco
- [ ] Pausa/reanudacion no cae la conexion Gemini
- [ ] Estado visual claro en todo momento
- [ ] Transiciones entre estados sin errores

---

## Etapa 4: Estabilidad y Calidad

### Objetivo
Manejo de errores robusto, reconexion automatica, y logging para debugging.

### Tareas

| # | Tarea | Estimacion |
|---|-------|------------|
| 4.1 | Implementar `GeminiSession.reconnect()`: si la sesion se cae, esperar con backoff exponencial y reconectar | 1h |
| 4.2 | Manejar errores de audio: dispositivo desconectado, buffer overflow/underflow | 45 min |
| 4.3 | Agregar logging a archivo con niveles configurable (DEBUG a archivo, INFO a consola) | 30 min |
| 4.4 | Agregar health checks: si no se recibe audio de Gemini por > 10s, forzar reconexion | 30 min |
| 4.5 | Agregar estadisticas al finalizar: tiempo total, bytes enviados/recibidos, reconexiones | 30 min |
| 4.6 | Manejar graceful shutdown: cerrar streams de audio al detener | 15 min |
| 4.7 | Test de resistencia: sesion de 30+ minutos sin caidas | 1h |

### Verificacion
1. Ejecutar `python main.py` con `--log-level DEBUG`
2. Forzar desconexion (desconectar WiFi momentaneamente)
3. Verificar que reconecta automaticamente con backoff
4. Revisar `logs/translator.log` para ver el detalle de la reconexion
5. Sesion de 30 minutos sin intervencion manual

### Criterio de aceptacion
- [ ] Reconexion automatica funciona tras caida de red
- [ ] Backoff exponencial: 1s -> 2s -> 4s -> 8s -> ... -> max 30s
- [ ] Logs utiles para debugging (timestamp, nivel, componente, mensaje)
- [ ] No se pierden mas de 5 segundos de audio en una reconexion
- [ ] Estadisticas de sesion al finalizar

---

## Etapa 5 (Opcional): UI Minima con Indicadores

### Objetivo
Reemplazar la UI de consola con una ventana simple que muestre indicadores visuales de actividad en ambos canales.

### Tareas

| # | Tarea | Estimacion |
|---|-------|------------|
| 5.1 | Evaluar `tkinter` vs `PyQt6` vs `textual` (TUI) | 30 min |
| 5.1 | Ventana con: indicador de estado (led verde/rojo/amarillo), barras de volumen A/B | 1h |
| 5.2 | Mostrar ultima transcripcion recibida en cada direccion | 30 min |
| 5.3 | Botones Start/Stop como alternativa a hotkeys | 30 min |
| 5.4 | Selector de idiomas (para cambiar direcciones de traduccion) | 30 min |

### Criterio de aceptacion
- [ ] Ventana responde a hotkeys y clicks de botones
- [ ] Indicadores de volumen actualizan en tiempo real
- [ ] Transcripciones visibles sin scroll manual

---

## Resumen de hitos

| Etapa | Tiempo estimado | Entregable |
|-------|----------------|------------|
| 1 | 2-3 horas | Pipeline unidireccional funcional |
| 2 | 3-4 horas | Pipeline bidireccional funcional (MVP real) |
| 3 | 2-3 horas | Control por hotkeys + estados |
| 4 | 3-4 horas | Sistema robusto para uso diario |
| 5 | 2-3 horas | UI visual |
| **Total** | **12-17 horas** | Producto completo |
