# Aristas de JSX (#87, v1.5) — diseño

## Estado

- **Propuesta del 2026-11-28, pendiente de aprobación.** No se ha cambiado slurp: todo lo medido
  sale de un prototipo fuera del paquete, en `docs/design/jsx-v1.5/`.
- **El criterio del banco lo fijó el autor antes de medir** (sección 5). Este documento se
  registra antes de ejecutar el banco; los resultados irán en `jsx-v1.5/resultados/`.

## El problema

`<Badge />` no crea ninguna arista: solo `Badge()` es una llamada. En un proyecto React casi todos
los componentes salen LOW en `explain`, `impact`, `diff` y en la sugerencia de riesgo.

- PrismaStats (grafo de `slurp index` en `e975140`): **530 de los 536 nodos** de ficheros `.tsx`
  (sin imports ni tests) son LOW, 5 MEDIUM y 1 HIGH.
- excalidraw (`dabd83c`): 796 de 854 LOW.
- graphify da 16 dependientes a `Badge()`, pero por aristas `imports` desde los ficheros, no por
  JSX: graphify no tiene convención para el JSX, así que la compatibilidad no condiciona el nombre.

## 1. Cómo representar el uso de un componente

**Hechos del código** (no hay que suponer nada):

- Las **bandas de riesgo** de `explain`, `impact`, `diff` y `--suggest` cuentan como dependiente a
  cualquier predecesor que no sea `contains` ni un nodo import (`_graphutils._split_callers`),
  sea cual sea la relación. Cualquier relación nueva cuenta para el riesgo sin tocar nada.
- `diff` marca un nodo como cambiado si gana o pierde cualquier arista saliente (no mira la
  relación), y lee las bandas igual que arriba.
- Dependen de `calls` en concreto:
  - el paseo del PageRank personalizado de la v1.4 (`scorer._walk_lists`: `calls` en los dos
    sentidos y `contains`);
  - el resolutor entre ficheros de TS (`_resolve_ts_cross_file_calls` solo reescribe `calls`; otra
    relación que apuntara a un nodo import sobreviviría apuntándole);
  - `_call_graph_gap` (si una extensión tiene grafo de llamadas). `.tsx`/`.jsx` ya están en la
    lista fija, así que no cambia.
- La selección (`build_context`) usa todas las aristas: el impulso de vecino recorre
  predecesores y sucesores de cualquier relación, y toda arista entre nodos seleccionados es un
  elemento que se puede incluir (peso 1,0 salvo `contains`/`imports_from`/`imports`/`defines`).
- **Una arista por par.** El grafo es un `DiGraph` y `_assemble_graph` deduplica por
  (origen, destino) sin mirar la relación: gana la primera emitida. Hoy `contains` gana a `calls`.

**Opciones:**

| | A. `calls` | B. `renders` (relación nueva) |
|---|---|---|
| Bandas de riesgo (explain, impact, diff, suggest) | cuentan | cuentan (sin tocar nada) |
| `diff` | igual | igual |
| Orden (paseo del PPR) | cuenta sin tocar nada | cuenta si se añade a `_walk_lists`; **se puede dejar fuera** |
| Selección (impulso, aristas elegibles) | cuenta | cuenta, salvo que la consulta filtre la relación |
| Salida | `- page → calls → Badge` | `- page → renders → Badge` |
| Resolutor entre ficheros | sin cambios | aceptar `renders` además de `calls` |
| Separar riesgo y orden si el banco lo pide | no, sin una marca aparte | sí: la consulta ignora `renders` |
| Formato `graph.json` | igual | una cadena de relación más; el formato no cambia |

**Recomendación: B, `renders`, contada donde cuenta `calls`** (en el paseo del PPR con los mismos
pesos). Dice al modelo lo que pasa (una página renderiza un componente; no lo llama con
argumentos), y deja la palanca para separar riesgo y orden si el banco la necesita (sección 5).

**Precedencia por par:** `contains` > `calls` > `renders`. Un componente anidado que su padre
renderiza conserva `contains`, como ya pasa con las llamadas; uno que además se llama como función
(`renderRow()`) conserva `calls`.

## 2. Qué casos cubre

**Regla general, como el resto del grafo de llamadas: cero falsos positivos.** Un uso da arista
solo si el nombre resuelve con certeza a un nodo función o clase del proyecto. Lo demás es un hueco.

| Caso | Se cubre | Cómo |
|---|---|---|
| `<Badge />` definido en el mismo fichero | sí | la misma búsqueda léxica que las llamadas (`_lookup_name`) |
| `<Badge />` importado de otro fichero | sí | arista pendiente hacia el nodo import, resuelta entre ficheros como las llamadas |
| `import Badge from "./badge"` (default) | sí, si el símbolo se llama como el binding | la regla que ya usan las llamadas |
| `<Dialog.Root />` con `import * as Dialog` | sí | la propiedad es el símbolo, como `db.f()` |
| `<Foo.Bar />` con otro objeto (compuestos `Object.assign`, estáticos) | no | no hay nodo `Foo.Bar` |
| Componente pasado como prop (`icon={Badge}`) | no | es una referencia, no un renderizado (las funciones pasadas como valor tampoco son llamadas); `element={<Home />}` sí, porque es JSX |
| `React.lazy(() => import("./x"))` | sí | import dinámico resuelto como un default; con `.then(m => m.X)`, el símbolo `X` |
| `next/dynamic` | sí | igual que `lazy` |
| `const X = forwardRef(...)` / `memo(...)` | **decisión pendiente (W2)** | hoy no son nodos; ver abajo |
| Etiquetas en minúscula (`<div>`, `<svg:rect>`), fragmentos | no | elementos intrínsecos |
| Componentes de librerías (`lucide-react`, `recharts`…) | no | un *bare specifier* nunca resuelve (`_ts_resolve_module_path`) |
| Nombre que no es local ni importado | no | global o inventado |
| JSX fuera de toda función (`root.render(<App />)`) | tree-sitter: arista desde el módulo, como las llamadas de nivel de módulo; regex: no | |
| JSX en ficheros `.js` | no | se parsean con la gramática de TypeScript, que no admite JSX: es otro asunto |

**W2: componentes envueltos en `memo`/`forwardRef`.** Hoy `const Button = React.forwardRef(...)`
no es un nodo (solo lo son los valores flecha o `function`), así que un JSX que lo usa no tiene a
qué apuntar. Son justo los componentes más usados: en PrismaStats los de shadcn/ui (`Button`, 119
usos; `Input`, 80; `Label`, 79); en excalidraw `Excalidraw = React.memo(...)` (317) e `IconButton`.

- Regla W2 (tree-sitter): `const X = W(fn)` con `W` = `memo`/`forwardRef` importado de `"react"`, o
  `React.memo`/`React.forwardRef` con `React` importado de `"react"`, y `fn` una función (o otro
  envoltorio de una) → `X` es un nodo función y `fn` es su ámbito (sus llamadas y su JSX son de
  `X`). `const X = memo(Y)` → nodo `X` y arista `X → Y`.
- **Añade nodos**, así que cambia el orden por sí solo. Se mide aparte (W1 sin nodos nuevos, W2 con
  ellos) y la decide el autor.

**Las dos ramas.**

- tree-sitter: un visitante que recorre el árbol con la misma pila de ámbitos que
  `_TSCallVisitor` y resuelve `jsx_opening_element` y `jsx_self_closing_element`.
- regex: `<Nombre` o `<Ns.Nombre` donde puede empezar un elemento (no tras un identificador, `.`,
  `)` o `]`), resuelto con las mismas tablas que las llamadas regex; lo que cae fuera de todo cuerpo
  que la regex sabe delimitar no da arista.
- **Dos fallos de la rama regex, encontrados al comparar las ramas** (afectan ya a las llamadas
  regex, que es lo que obtiene quien instala sin el extra `ts`):
  1. `_ts_blank_noise` convierte en espacios los saltos de línea de los comentarios y *templates*
     de varias líneas, así que las líneas del texto limpio se desplazan y `_ts_decl_offset` busca
     la declaración en otra línea; si no encuentra el nombre, devuelve el final de esa línea en
     vez de rendirse. En PrismaStats, 197 declaraciones se buscan en una línea equivocada (70
     aristas `calls` atribuidas desde ellas); en excalidraw, 1.374 (329). Era la causa de una
     arista falsa del prototipo (`App.isIframeLikeElementCenter → Spinner`).
  2. `_ts_body_span` toma la primera `{` tras el nombre, que en `function Page({ a })` es el
     parámetro desestructurado (la forma normal de un componente), y no reconoce los cuerpos
     `=> ( … )`.

  Se propone arreglar los dos, para llamadas y JSX, como un commit aparte.

## 3. Cuánto cambia el grafo (medido en el HEAD del banco)

Prototipo W1 (tree-sitter), sobre el grafo guardado y el fuente en ese HEAD:

| | PrismaStats | excalidraw |
|---|---|---|
| Ficheros `.tsx`/`.jsx` | 124 | 329 |
| Elementos de componente | 1.623 | 1.433 |
| Aristas nuevas | 151 (49 locales, 93 importadas, 9 `dynamic`) | 320 (73 locales, 247 importadas) |
| Usos que ya tenían arista (`calls` o `contains`) | 81 | 192 |
| Imports de librerías externas (bien ignorados) | 714 (`lucide-react` 337, `@react-pdf` 143, `recharts` 86…) | 96 |
| Imports del proyecto cuyo símbolo no es un nodo | 644 (`forwardRef` de shadcn/ui) | 536 (`React.memo`, `forwardRef`, compuestos) |
| `<Obj.Miembro>` sin namespace | 10 | 251 |
| Bandas: LOW → MEDIUM | 10 | 37 |
| Bandas: LOW → HIGH | 2 | 8 |

Rama regex (con los dos arreglos): PrismaStats 122 aristas, 121 iguales a tree-sitter y 1 con
atribución más gruesa (la función que contiene a la anidada, como hacen las llamadas regex);
excalidraw 74, todas iguales.

La reconstrucción de la parte TS que usa W2 reproduce exactamente el grafo guardado sin la regla
(nodos y aristas, atributos incluidos) en PrismaStats, excalidraw, hono y prometheus.

Las cifras de W2 en estos grafos y el banco: pendientes (sección 5).

## 4. Qué proyectos del banco pueden cambiar

Solo los que tienen `.tsx`/`.jsx`: PrismaStats (124 módulos en el HEAD) y hono (31), de derivación;
excalidraw (329) y prometheus (149, su `web/ui`), de validación. En los otros diez (sqlalchemy,
commons-lang, tokio, caddy, jsoup, ripgrep, click, gin, ky, slurp) las aristas nuevas son cero por
construcción; se comprueba que ningún grafo padre suyo tiene ficheros `.tsx`/`.jsx`.

## 5. Efecto en el orden: criterio del autor (2026-11-28), fijado antes de medir

- **Se ejecuta el banco de 1.525 preguntas con y sin el cambio** (W0 sin JSX; W1 con las aristas
  `renders` en el paseo; W2 con W1 más los nodos de `memo`/`forwardRef`), con el código de la v1.4.
- **Criterio:** el recall medio a 4000 en markdown de los 6 proyectos de validación no puede bajar
  más de **0,01**, ni ningún proyecto más de **0,03**.
  - Lectura de «ningún proyecto»: cualquiera de los 14, derivación incluida (la lectura más
    estricta). Si solo falla en derivación, se informa de las dos lecturas.
  - Los juegos etiquetados (PrismaStats 15, slurp 16) se informan aparte.
- **Si baja más:** se separan riesgo y orden. Las aristas `renders` siguen contando para las bandas
  de `explain`, `impact`, `diff` y la sugerencia de riesgo, pero la consulta las ignora: ni paseo
  del PPR, ni impulso de vecino, ni aristas elegibles, ni salida de `slurp_query`. Entonces el
  recall es el de sin JSX por construcción.
