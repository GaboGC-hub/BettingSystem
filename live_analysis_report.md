# Reporte del Rendimiento Histórico (`live_history`)

He programado un script personalizado (`analyze_history.py`) para consumir todos los 52 archivos `.jsonl` alojados en tu carpeta temporal y reconstruir el destino final de cada uno de los partidos en crudo frente a las "señales" que el modelo arrojó en vivo (`best_side` con `NO BET` discriminado). 

Aquí están los datos y el diagnóstico de rentabilidad real que esconde el sistema.

## 📊 Las Estadísticas Reales (Validación de Backtest)

He evaluado qué ocurrió **exactamente** al final del partido (usando los goles, corners y tarjetas finales reportados) frente a la línea sugerida y la decisión.

**Muestra**: 52 Partidos
**Volumen de Apuestas Sugeridas**: 254 entradas únicas (excluyendo duplicados ciegos)
**Win-Rate Global**: 66.54% (169 Ganadas - 85 Perdidas)

### Desglose Quirúrgico por Mercado y Lado (OVER vs UNDER)

**⚽ GOLES** (Win Rate Global: 67.95%)
- **✅ UNDER**: 73.2% de aciertos (41 apuestas)
- **⚠️ OVER**: 62.2% de aciertos (37 apuestas)
- *Diagnóstico:* Muy sólido. El Poisson capta la sequía goleadora magistralmente, aunque el OVER penaliza un poco.

**🟨 TARJETAS** (Win Rate Global: 70.73%)
- **✅ UNDER**: 74.1% de aciertos (54 apuestas)
- **✅ OVER**: 64.3% de aciertos (28 apuestas)
- *Diagnóstico:* El mercado más fuerte. Tu modelo actual lee excelentemente la tensión, y nuestra corrección (Hard Lock y Filtro de Faltas reciente) blindará aún más el *Under*, que de por sí ya es una máquina de 74.1% de win rate.

**🚩 CORNERS** (Win Rate Global: 61.70%)
- **✅ UNDER**: 78.8% de aciertos (52 apuestas)
- **🆘 OVER: 40.5% de aciertos** (42 apuestas) *(Rendimiento crítico - Sangrado de Bankroll)*
- *Diagnóstico:* **Aquí está la fuga de capital.** El modelo es espectacular detectando partidos "muertos" que no darán más corners (78.8% de acierto), pero se inventa corners imaginarios cuando sugiere OVERs.

---

## 🎯 ¿Qué debemos mejorar urgentemente en el modelo?

El análisis de datos grita 3 cosas que hay que implementar:

1. **Cortar la hemorragia del OVER Corners (40.5% Win-Rate):**
   - El modelo actual confía demasiado en la "Regla de la Desesperación" (equipo perdiendo = lluvia de corners).
   - *La cura:* Restringir las apuestas de OVER Corners. El modelo DEBE exigir un número mínimo innegociable de Tiros a Puerta y Centros continuos (`tiros_puerta_local` elevado y `centros` que agregamos en el commit anterior). Si no hay llegadas peligrosas de verdad, el OVER corner se castiga y pasa a `NO BET`.

2. **Incrementar la exigencia Base de Probabilidad para GOLES (OVER):**
   - El *OVER Goles* está en apenas 62.2%. A cuotas de 1.80, rozas el punto de equilibrio (break-even). Necesitamos subir el límite base del cálculo de *EV* y *Probabilidad Fair* de Goles para que filtre escenarios mediocres.

3. **Demasiada hiperactividad (Volumen de Entradas):**
   - Sacar 254 recomendaciones en 52 partidos es altísimo (casi 5 apuestas per match). Aunque logres 66.5% general, es volumen tipo "metralleta" (scattergun), lo cual choca rápido con límites antispam en Betplay.
   - *La cura:* Gracias a nuestro `Cooldown Hard Lock` de 10 minutos (que ya agregamos) frenaremos este spam sin perder los mejores *Value Bets*.

El gran descubrimiento es la asimetría del modelo: **Tu sistema es buenísimo detectando congelamientos (UNDER), pero es terrible forzando el acelerador en ataques estériles (OVER Corners)**.

¿Quieres que iniciemos una fase de "poda" estricta en la lógica matemática del `update_match_math` y `apply_market_guardrails` para castigar las proyecciones de corners infladas y subir los thresholds de EV?
