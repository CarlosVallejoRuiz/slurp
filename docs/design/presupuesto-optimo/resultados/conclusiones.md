# Presupuesto óptimo: resultados y conclusiones (2026-11-26)

**Ficheros:**

- el diseño, con el criterio fijado antes de medir: `../../presupuesto-optimo.md`;
- todas las cifras: `tablas.md`, generadas desde `resumen.json`;
- las curvas: `curvas-markdown.svg` y `curvas-json.svg`;
- la puerta de preguntas: `puerta.md`.

**Qué se ejecutó.**

- slurp v1.3.0, sin tocar su código.
- 14 proyectos y **1.525 preguntas de commits**: 120 por proyecto, salvo PrismaStats (68),
  slurp (38) y ripgrep (99), cuyos historiales no daban para más.
- Además, los dos juegos de referencia etiquetados (15 + 16 preguntas).
- 11 presupuestos de 1000 a 32000 y el control de 64000, en markdown y JSON, más las 18
  combinaciones del corte por relevancia en cada techo.
- 34 minutos con 4 procesos.
- El arnés coincidió con `run_retrieval_eval` en las dos primeras preguntas de cada proyecto.

## Resumen

1. **Con preguntas que piden encontrar el código que hay que cambiar, el recall no satura en
   ningún presupuesto razonable.**
   - Sube de forma casi uniforme con el logaritmo de los tokens: unos **10 puntos cada vez
     que se duplica** el presupuesto, de 1000 a 32000, en los dos formatos.
   - Solo «satura» en los proyectos pequeños, y lo hace cuando caben enteros todos los
     candidatos.
2. **Según el criterio fijado, la regla se acepta.** Pero eso es casi solo un efecto de la
   censura, así que **no recomiendo implementarla**.
   - La regla es B ≈ 850·√C, que da 32000 para cualquier proyecto de más de unos 800
     candidatos.
   - Se ajustó con los tres únicos proyectos de derivación que saturan en markdown: ky,
     PrismaStats y hono, los tres en TypeScript.
   - Una constante de 32000 también pasaría la validación, con coste 1,33× frente al 1,27× de
     la regla.
3. **El corte por relevancia no sirve tal como se diseñó.** Ninguna de sus 18 combinaciones
   cumple los márgenes en derivación, así que no hubo nada que validar.
4. **El 4000 actual encuentra de media el 54–56 % de las definiciones cambiadas en markdown**,
   frente al 81–84 % en el óptimo de cada proyecto. Pierde 28 puntos de media y gasta la sexta
   parte de tokens.
5. **La palanca medida no es el presupuesto, es el orden.**
   - El primer relevante cae, en mediana, entre el puesto 4 (commons-lang) y el 270 de los
     candidatos; en 10 de los 14 proyectos, más allá del 20.
   - Con las preguntas etiquetadas cae en el puesto 3,5–4.
   - Más presupuesto solo mete más grafo.

## Curvas y punto de saturación

Recall medio en markdown, con tokens reales de alrededor del 98 % del presupuesto:

| Proyecto | Grupo | C (candidatos) | 4000 | 8000 | 16000 | 32000 | 64000 | B\* md | B\* JSON | ¿Satura en 32k? |
|---|---|---|---|---|---|---|---|---|---|---|
| ky | D | 52 | 0,90 | 0,99 | 1,00 | 1,00 | 1,00 | 6000 | 12000 | sí, todo cabe |
| PrismaStats | D | 567 | 0,41 | 0,53 | 0,86 | 1,00 | 1,00 | 24000 | 32000\* | md sí, todo cabe |
| hono | D | 1.012 | 0,67 | 0,77 | 0,89 | 1,00 | 1,00 | 24000 | 24000\* | md sí, todo cabe |
| slurp | D | 715 | 0,77 | 0,88 | 0,91 | 0,93 | 1,00 | 12000\* | 16000\* | no |
| jsoup | D | 2.442 | 0,53 | 0,66 | 0,75 | 0,81 | 0,90 | 24000\* | 24000\* | no |
| caddy | D | 2.185 | 0,38 | 0,53 | 0,61 | 0,73 | 0,90 | 32000\* | 24000\* | no |
| tokio | D | 5.794 | 0,46 | 0,58 | 0,66 | 0,76 | 0,80 | 24000\* | 32000\* | no |
| sqlalchemy | D | 12.448 | 0,37 | 0,45 | 0,57 | 0,68 | 0,70 | 32000\* | 32000\* | no |
| gin | V | 448 | 0,78 | 0,90 | 1,00 | 1,00 | 1,00 | 12000 | 24000 | sí, todo cabe |
| click | V | 693 | 0,61 | 0,76 | 0,87 | 1,00 | 1,00 | 24000 | 32000\* | md sí, todo cabe |
| ripgrep | V | 2.796 | 0,42 | 0,51 | 0,65 | 0,79 | 0,93 | 32000\* | 32000\* | no |
| excalidraw | V | 2.430 | 0,43 | 0,54 | 0,69 | 0,77 | 0,82 | 24000\* | 32000\* | no |
| commons-lang | V | 4.868 | 0,68 | 0,73 | 0,75 | 0,81 | 0,87 | 24000\* | 12000\* | no |
| prometheus | V | 6.696 | 0,34 | 0,45 | 0,52 | 0,59 | 0,67 | 24000\* | 32000\* | no |

\* Celda censurada: entre 32000 y 64000 el recall aún sube más de 2 puntos. Su B\* es una cota
inferior.

**Ganancia media por duplicación** (todas las celdas):

| Tramo | Markdown | JSON |
|---|---|---|
| 1k→2k | +0,12 | +0,09 |
| 2k→4k | +0,10 | +0,11 |
| 4k→8k | +0,11 | +0,10 |
| 8k→16k | +0,11 | +0,11 |
| 16k→32k | +0,08 | +0,10 |

**«Satura» quiere decir que todo cabe.** Los cinco proyectos que saturan en markdown (ky,
PrismaStats, hono, gin, click) lo hacen cuando el presupuesto da para casi todos sus
candidatos:

- en 32000 caben de 540 a 890 nodos en los proyectos que no se agotan antes;
- y estos cinco tienen 52, 567, 1.012, 448 y 693 candidatos.

En los proyectos con 2.000 o más candidatos, 32000 cubre entre el 7 y el 35 % y el recall sigue
subiendo. No hay un punto, por debajo de 32000, a partir del cual más tokens dejen de encontrar
cosas nuevas.

## Comprobación del método

Se compara el B\* de las preguntas de commits con el de los juegos etiquetados:

| Proyecto | Markdown | JSON |
|---|---|---|
| PrismaStats | 24000 frente a 3000: 6 pasos | 32000 frente a 6000: 5 pasos |
| slurp | 12000 frente a 16000: 1 paso | 16000 frente a 16000: 0 pasos |

La puerta solo para si las cuatro celdas se separan más de 2 pasos, así que **no paró**. La
diferencia de PrismaStats es real y se explica: son tareas distintas.

- **Las preguntas etiquetadas de PrismaStats son de arquitectura.** Por ejemplo: la función más
  llamada, o qué crea el cliente de administración. Sus relevantes son nodos centrales: el
  primero cae, en mediana, en el puesto 4.
- **Las de commits piden la función concreta que un cambio tocó.** En PrismaStats, el primer
  relevante cae en mediana en el puesto 108.
- **Las de slurp las etiquetó Claude Code a nivel de función**, como las de commits, y coinciden.

Conclusión: el óptimo depende de qué se pregunta. Para orientarse en la arquitectura basta con
poco; para encontrar el código que hay que cambiar, no hay techo dentro de 32000.

## La regla, contra su criterio en validación

**Cómo se eligió.**

- Ajuste en derivación, con los proyectos de 40 preguntas o más. slurp quedó fuera con 38.
- Se eligió R1 con C (candidatos): log₂ B = 9,730 + 0,502·log₂ C, redondeado hacia arriba a la
  rejilla.
- JSON: factor k = 2,0. La validación cruzada no podía elegir entre factor y ajuste propio:
  solo ky satura en los dos formatos.

**Validación.**

| Medida | Resultado | Límite |
|---|---|---|
| Pérdida máxima | 0,000 | 0,03 |
| Pérdida media | 0,000 | 0,01 |
| Coste geométrico | 1,27× | 1,5× |
| Coste máximo | 2,7× | 4× |

**Pasa.** La constante R0 (16000 / 12000) no pasa: pierde 0,195 como máximo. Así que, según
§4.3, la recomendación formal sería **la regla**.

**Por qué no la recomiendo.**

- **Se ajustó en la práctica con 3 puntos.** Son las tres únicas celdas sin censurar de
  derivación en markdown, ky, PrismaStats y hono, todas en TypeScript. Para JSON, con uno.
- **Su veredicto en validación lo decide la censura.**
  - Para casi todos los proyectos la regla da 32000, el techo de la rejilla.
  - Los B\* de validación están casi todos en 24000–32000, y la mayoría son cotas inferiores.
  - Así, la pérdida 0 es la de dar el máximo. Una constante de 32000 da exactamente lo mismo
    (pérdida 0, coste 1,33×).
- **«32000 tokens por consulta» no es un óptimo, es un precio.** Son 8 veces el 4000 actual por
  cada llamada, a cambio de unos 25–30 puntos de recall en los proyectos grandes.

## El corte por relevancia

**Cómo se midió.** El techo fue el presupuesto de la regla. Ninguna de las 18 combinaciones
cumple en derivación los márgenes de la regla (0,03 por celda, 0,01 de media):

| Corte | Ahorro de tokens | Pérdida media | Pérdida máxima |
|---|---|---|---|
| Los que ahorran 70–98 % | 70–98 % | 0,34–0,65 | — |
| C3 con γ 0,2 | 9 % | 0,053 | 0,20 |
| C3 con γ 0,3, el más suave | 1 % | 0,010 | 0,055 |

**Por qué falla.**

- Los relevantes de muchas preguntas no comparten términos con el mensaje del commit. Quedan en
  el suelo de scores, 0,30, con el resto del grafo.
- slurp solo los alcanza por el impulso de sus vecinos o por PageRank, cuando el presupuesto deja
  entrar muchos nodos del suelo.
- Un umbral sobre el score propio los deja fuera: no son candidatos.

El ~98 % de llenado no es desperdicio en estas preguntas: con ese relleno se encuentra una parte
del recall.

## El 4000 actual

| Grupo | Recall en 4000 (md / JSON) | Recall en B\* (md / JSON) | Pérdida media | Pérdida máxima | Tokens frente a B\* |
|---|---|---|---|---|---|
| Derivación | 0,56 / 0,45 | 0,84 / 0,74 | 0,28 | 0,58 (PrismaStats md) | 0,19× |
| Validación | 0,54 / 0,43 | 0,81 / 0,72 | 0,28 | 0,39 | 0,17× |

**Qué depende de la pregunta.**

- **Si la pregunta nombra la definición** (`nombra_símbolo`), el recall en 4000 va de 0,56 a
  1,00 según el proyecto.
- **Si no la nombra**, baja a 0,23–0,89.
- **Con las preguntas de arquitectura de PrismaStats**, 4000 basta: 0,97.

## Recomendación

1. **No cambiar el presupuesto por defecto por una regla de tamaño, ni implementar el corte.** Lo
   que el estudio buscaba, el menor presupuesto por encima del cual no se encuentra nada nuevo,
   no existe por debajo de 32000 para proyectos de más de unos 2.000 candidatos. La regla
   aceptada equivale a «32000 a partir de unos 800 candidatos».
2. **Mantener 4000** en CLI y MCP. Es un punto razonable para la primera consulta con la que un
   agente se orienta: en la medición con agentes, la primera llamada fue siempre `slurp_query`, y
   luego trabajaban con grep y lecturas. Cada duplicación cuesta el doble de contexto por ~10
   puntos.
3. **Corregir la guía de presupuesto de `slurp_query`**, como cambio aparte cuando lo decidas.
   Hoy dice «8000–12000 for broad architecture questions», y los datos dicen casi lo contrario:
   - la arquitectura se resuelve con poco;
   - lo que pide presupuesto es localizar la función exacta que cambiar en un proyecto grande:
     16000 encuentra del 52 al 75 %, y 32000 del 59 al 81 %.
4. **Lo que de verdad movería las curvas es el orden de los resultados, no el presupuesto.** Este
   estudio deja un banco reutilizable: 1.525 preguntas sin etiquetar a mano, con grupos de
   derivación y validación fijos y un arnés que mide en 34 minutos. La métrica a mover es el
   recall en 4000 de las preguntas que no nombran el símbolo. Candidatos:
   - BM25 o docstrings (la rama #21);
   - aprovechar los identificadores de la consulta;
   - que el corte use la relevancia con el impulso de los vecinos.

## La comprobación con un LLM

**Estado: preparada, no lanzada.** `llm_check.py prepare` dejó 240 prompts en
`~/slurp-bench/llm/prompts.jsonl` sin llamar a `claude`. `run --launch` no se ha ejecutado.

**Antes de lanzarla, dos problemas:**

- **El volumen.** Las respuestas suman 9,4 M tokens de entrada en `cl100k`, unos 14,7 M de
  Anthropic, más el juez. Es mucho para la suscripción.
- **2·B\* y 4·B\* coinciden.** Con B\* = 24000–32000, los dos se redondean a 64000 en hono, tokio
  y sqlalchemy. El diseño no puede distinguirlos, y además compara un B\* que es casi siempre el
  techo de la rejilla.

**Recomendación:** no lanzarla tal como está. Si se quiere saber si un modelo responde mejor con
más contexto, la pregunta útil es 4000 frente a 16000 frente a 32000, con las mismas 60
preguntas: unos 3,1 M tokens de entrada. Pero es un cambio de diseño, y lo decide el autor.

## Desviaciones respecto al diseño, todas antes de ver la validación

- **Mínimo de 40 preguntas.** El diseño lo fija (§2.4), pero el análisis no lo aplicaba; se
  corrigió antes de ejecutarlo, y slurp queda fuera de los ajustes.
- **JSON sin validación cruzada posible.** Se usa el factor k, la opción simple que el diseño
  da por defecto (§4.2).
- **Gráficas.** Solo cambia la disposición de los SVG; las cifras son idénticas.

## Límites

- **Censura.** La mayoría de las celdas no saturan en 32000. B\* es una cota inferior, y la
  validación de la regla depende de eso.
- **El sustituto.** Los mensajes de commit piden «dónde cambiar», no «cómo funciona». El estudio
  mide esa tarea, que es más difícil que la de los juegos etiquetados de arquitectura.
- **Qué cuenta como relevante.** Solo las definiciones cambiadas, no todo el contexto necesario
  para entenderlas.
- **Una ejecución.** slurp es determinista y sin LLM, así que la incertidumbre viene de qué
  preguntas salen; el bootstrap la da en `tablas.md`.
