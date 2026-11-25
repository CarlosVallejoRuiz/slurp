# CLAUDE.md — Proyecto Slurp

## Qué es Slurp

Slurp es una herramienta CLI open source de **token-budget-aware graph navigation** para AI coding agents.

**El problema:** Los knowledge graphs (como los que genera graphify) pueden tener miles de nodos. Inyectar el grafo entero en el contexto de un LLM es ineficiente y caro. Slurp selecciona inteligentemente el subgrafo más relevante que cabe dentro de un presupuesto de tokens dado.

**La metáfora:** Un bowl de ramen es un knowledge graph — miles de nodos entrelazados. Slurp extrae exactamente los noodles que necesita tu LLM, sin traerse el bowl entero.

> *"graphify builds the bowl. slurp serves exactly the noodles your LLM needs."*

**Propuesta de valor única:**
- Compatible con `graph.json` de graphify (formato principal) y cualquier grafo estándar
- Dado un budget en tokens y una query, devuelve el subgrafo óptimo
- Trazabilidad completa: audit log de qué nodos se usaron en cada query
- Funciona como CLI standalone, como librería Python importable, y como servidor MCP
- Publica en PyPI como `slurp-graph`, el comando CLI es `slurp`

**Autor:** Juank (Juan Carlos Vallejo Ruiz) — Full-Stack Developer & AI entrepreneur, Terrassa, Barcelona.
**Repo:** github.com/CarlosVallejoRuiz/slurp
**PyPI:** slurp-graph

---

## Stack técnico

- **Python 3.12+**
- **uv** para gestión de dependencias, packaging y ejecución
- **networkx** para operaciones de grafo (PageRank, traversal, subgraph)
- **tiktoken** para conteo de tokens preciso (cl100k_base por defecto)
- **rich** para CLI output con tablas, paneles y colores
- **click** para CLI interface
- **pathspec** para ignore patterns en `.slurpignore`
- **watchdog** para `--watch` mode en `slurp index`
- **anthropic** SDK (opcional en v1, para embeddings en v4)
- **tree-sitter** + gramáticas por lenguaje — **extras opcionales**, nunca requeridos
- Tests con **pytest**, linting con **ruff**, formatting con **black**

### Lenguajes soportados por `slurp index`

| Lenguaje | Parser | Extra |
|---|---|---|
| Python | stdlib `ast` | — (siempre disponible) |
| TypeScript / JavaScript | tree-sitter | `ts` |
| Java | tree-sitter | `java` |
| Rust | tree-sitter | `rust` |
| C# | tree-sitter | `csharp` |
| Ruby | tree-sitter | `ruby` |
| PHP | tree-sitter | `php` |
| Kotlin | tree-sitter ⚠️ | `kotlin` |
| Scala | tree-sitter | `scala` |
| Swift | tree-sitter | `swift` |
| C | tree-sitter | `cpp` |
| C++ | tree-sitter | `cpp` |
| Go | regex | — (regex es el parser principal) |
| Lua | regex | — (regex es el parser principal) |
| Elixir | regex | — (regex es el parser principal) |
| PowerShell | regex | — (regex es el parser principal) |

⚠️ **Kotlin:** `tree-sitter-kotlin 1.1.0` falla al parsear una clase con cuerpo
seguida de un `object` — Kotlin corriente. La guarda `_tree_is_broken()` lo
detecta y en la práctica Kotlin se indexa por regex. El extra se mantiene
declarado para que una versión corregida de la gramática entre sin tocar código.

```bash
uv sync --extra all-languages   # las 10 gramáticas de golpe
uv sync --extra rust            # solo una
```

**Invariantes del indexador:**
- **Las reglas de `_SKIP_DIRS` se aplican a la ruta relativa a la raíz**, nunca a
  la absoluta. Antes un proyecto en `~/code/build/app` o `.../vendor/app` se
  indexaba a 0 nodos sin aviso (y `--smart` no veía cambios). `_should_skip`
  sigue mirando todas las partes de la ruta que recibe: son los llamadores
  (`iter_source_files`, `smart.get_changed_files`) quienes le pasan la relativa.
  `_vendor/` está en `_SKIP_DIRS` y los `*.min.js` se saltan estén donde estén:
  la v1.0.6 vendorizó vis-network y el índice de slurp ganó 118 nodos basura.
  Si un índice sale con 0 nodos, `slurp index` explica por stderr qué
  directorios saltados tenían fuentes (`skipped_source_files`) o que no hay
  ninguna extensión indexable.
- **Salidas de build de frameworks** (v1.1.1, #66): `.next`, `.nuxt`, `.svelte-kit`,
  `.turbo`, `.vercel`, `.output` en `_SKIP_DIRS` — solo nombres con punto que escribe la
  herramienta y que nunca contienen fuente (`next/`, `output/`… sí se indexan). `out/`
  solo se salta si contiene `_next/` (export estático de Next.js): mirarlo exige disco,
  así que `_should_skip(rel, root)` recibe la raíz y sin ella no salta `out/`;
  `_is_skipped_dir(nombre, ruta)` es la regla por directorio. PrismaStats: 1796 → 1582
  nodos, exactamente los 214 de `.next/` y sus 136 relaciones, nada más cambia.
- **Los directorios saltados se podan, no se recorren** (v1.1.1, #53): `_walk_files()`
  usa `os.walk` y recorta `dirnames` en sitio con `_is_skipped_dir`, así que
  `node_modules/` y compañía nunca se abren. Hasta v1.1.0 `rglob("*")` visitaba todo
  (PrismaStats: 50.136 rutas para 185 ficheros) y filtraba después. Mismo conjunto y
  mismo orden que antes (los dos ignoran directorios enlazados; los enlaces a fichero
  los juzga la comprobación de contención): lo fija
  `TestSkippedDirectoriesArePruned`, que espía `os.scandir` y compara con el
  algoritmo antiguo. PrismaStats, mediana de 5: descubrimiento 1.204 → 49 ms;
  `slurp index` 4,91 s (v1.1.0) → 2,89 s (#66) → 0,50 s (#53), grafo byte a byte
  idéntico al de solo #66. `slurp index` llama dos veces a `iter_source_files`
  (índice + resumen de parsers); con la poda la segunda cuesta ~50 ms.
- Cada gramática es un extra **independiente**: su ausencia es una configuración
  soportada y debe degradar en silencio al fallback regex.
- **Regex principal vs regex fallback**: Go, Lua, Elixir y PowerShell no tienen
  gramática cableada — su regex es el diseño, entran por `_index_regex_only()`,
  no intentan tree-sitter y `parser_summary()` los marca `is_fallback=False`.
  Marcarlos como fallback sería mentir sobre un grafo que no está degradado.
- Un fallo de parseo **con** la gramática instalada sí se avisa por stderr —
  los dos casos nunca comparten el mismo `except`.
- `slurp index` imprime siempre qué parser usó por lenguaje; un grafo degradado
  nunca debe pasar desapercibido. Con más de 4 lenguajes la línea se envuelve a
  3 por línea con 11 espacios de indentación (`format_parser_summary()`).
- **`_tree_is_broken(tree)`** — una gramática que devuelve un árbol con nodos
  `ERROR` no lanza excepción: el visitor la recorre y pierde declaraciones
  enteras en silencio. Tras **cada** parseo se comprueba `root_node.has_error`;
  si hay error se trata como fallo del parser → aviso a stderr + fallback regex.
  Aplica a los 8 lenguajes con tree-sitter, no solo a Kotlin. Nunca quitar esta
  guarda: es lo que convierte un fallo silencioso en uno visible y recuperable.
- Los visitors de tree-sitter heredan de `_BaseVisitor` (pila de scopes, `_emit`
  con atributos extra, dispatch `visit_<node_type>`). Añadir un lenguaje = una
  subclase + un fallback regex + una entrada en `_INDEXED_EXTENSIONS`,
  `parser_summary()` y `_LANGUAGE_BY_EXT` del CLI.
- Metadatos específicos por lenguaje: anotaciones/atributos (Java, C#),
  visibilidad (Java, Rust, C#, PHP), lifetimes (Rust), `is_async` (C#, Swift),
  `is_class_method` y accessors (Ruby), magic methods (PHP),
  `is_data_class`/`is_sealed`/`is_suspend`/`extends_type` (Kotlin),
  `is_case`/`is_implicit` y `def`/`val`/`var` como tipos (Scala),
  `is_computed` y protocolos (Swift), `is_header`/`is_template`/`is_extern_c`
  (C/C++), `is_local` y tables como módulos (Lua), `is_private` (Elixir),
  `verb`/`noun`/`has_params` (PowerShell). Edges tipados: `extends`, `implements`
  (incluido `impl Trait for Type` de Rust), `with` (Scala), `conforms_to`
  (Swift), `mixin` (Ruby `include`/`extend`/`prepend`, PHP `use` de traits),
  `use`/`import`/`alias`/`require` (Elixir) y `requires` (PowerShell).
- Los companion objects (Kotlin, Scala) se anidan bajo su clase — si no,
  colisionan con ella en el mismo node id.
- **Edges `calls` (Python, TypeScript/JavaScript, Go, Java, Rust)** — extracción en dos fases:
  recolectar *todas* las definiciones primero, resolver después. Un solo pase
  top-down no vale: `checkout` llama a `PaymentGateway.charge` antes de que esa
  clase aparezca. La política es **cero falsos positivos**: si una llamada no se
  resuelve con certeza (stdlib, terceros, método sobre objeto de tipo
  desconocido, callee calculado) no se emite edge. Un edge omitido es un hueco;
  uno incorrecto es una mentira sobre el código. Ambos extremos de todo edge
  `calls` se filtran contra los nodos realmente emitidos.
  - Un nombre suelto nunca resuelve contra un atributo de clase: dentro de un
    método, `foo()` es la función del módulo (necesita `self.`/`this.`).
  - La rama regex de TS ancla el cuerpo de cada definición por **número de
    línea** (`source_location`), no buscando el nombre por el fichero: una firma
    `abstract` sin cuerpo se quedaba con el `{` del método siguiente y le
    atribuía sus llamadas.
  - **Cross-file (solo Python)**: la pasada por fichero no puede resolver
    `from slurp.budget import select_subgraph`, así que emite el edge hacia el
    **nodo import** con el símbolo punteado en la clave privada
    `_pending_symbol`; `index_project()` lo reescribe al destino real o lo
    descarta. En el grafo terminado no sobrevive ninguna clave privada ni
    ningún edge `calls` hacia un nodo import.
    - **Garantizado en `_assemble_graph()`**: tras los cinco resolutores se
      quitan todos los atributos `_…` de nodos y edges. Hasta v1.0.7 sobrevivían
      en los nodos import de TS/JS, Go, Java y Rust. Lo vigila
      `TestNoPrivateAttributesInOutput` con los 16 lenguajes.
    - El id del nodo import usa el **scope actual**, no el módulo: un import
      perezoso dentro de una función vive en `cli.run.import_...`. Anclarlo al
      módulo hace que todos los edges apunten a nodos inexistentes y el filtro
      final los borre en silencio.
    - La mitad-módulo del import se resuelve contra un índice **solo de
      módulos** (`_build_module_index`). En el índice global el sufijo `audit`
      es ambiguo (módulo `audit` vs comando `cli.audit`) y se descarta,
      tumbando toda resolución desde ese módulo.
    - Un import solo resuelve si su **módulo** pertenece al proyecto. Sin esa
      guarda, `from pathlib import Path` se enlazaría con cualquier clase local
      llamada `Path`.
  - **Cross-file de TypeScript/JavaScript**: el nodo import lleva
    `_imported_symbols`, `_source_module` (ya resuelto a ruta punteada) e
    `_import_type` (`named`/`namespace`/`default`/`side_effect`). Las dos ramas
    —tree-sitter y regex— registran la misma metadata y producen los mismos
    edges.
    - **Cada resolutor toca solo sus imports.** El de Python corre primero; si
      consumiera los edges pendientes de TS los descartaría (un símbolo TS no
      lleva prefijo de módulo que resolver) y el resultado serían 0 edges
      cross-file con todo lo demás aparentemente correcto.
    - `_ts_resolve_module_path`: `./x` y `../x` contra el directorio del
      importador, `@/x` contra la raíz, **bare specifier → None**. Un paquete de
      node_modules nunca resuelve aunque exista un fichero local homónimo.
    - Se prueba también `{ruta}.index` (barrel exports) y se sanitizan los
      guiones igual que `_file_id` (`admin-actions` → `admin_actions`).
    - `import type {...}` y los `type X` en línea se ignoran: no existen en
      runtime, no pueden llamarse.
  - **Go** (regex-primario, sin gramática): los métodos se **anidan bajo su
    receiver** — `pkg.Struct.Method`, no `pkg.Method`. Antes el receiver se
    descartaba en un grupo no-capturador de `_GO_FUNC_RE`.
    - `_go_body_span()` cierra primero los **paréntesis de parámetros** y solo
      después busca el `{` del cuerpo. Con la búsqueda ingenua,
      `func F(cart map[string]interface{})` se queda con la llave de
      `interface{}` y el cuerpo sale de dos caracteres, perdiendo todas sus
      llamadas sin error.
    - Un `{` solo se salta si viene precedido del keyword `interface` o
      `struct`. Saltarlo por estar vacío hace que `func helper() {}` herede el
      cuerpo de la función siguiente y se le atribuyan **sus** llamadas — un
      falso positivo, no una omisión.
    - `pkg.Func()` solo resuelve si `pkg` es el receiver de la función
      envolvente o una variable local de tipo conocido; un paquete importado
      nunca resuelve.
    - **Limitación**: el tipo de retorno no se propaga. `srv := NewServer()`
      seguido de `srv.Start()` no genera edge. Sin cross-file todavía.
  - **Java** (tree-sitter + fallback regex): el lenguaje más barato de los
    cuatro — los tipos de variables y campos están **declarados**, no se
    infieren de un inicializador. Los métodos ya venían anidados bajo su clase
    (`Fichero.Clase.metodo`); los node ids usan el **stem del fichero**, no el
    paquete.
    - Precedencia del receptor: **variable local → campo → nombre de clase**
      (estático). Un local `Holder Gw` gana al tipo `Gw` del mismo nombre.
    - Los campos se recolectan **antes** de recorrer ningún cuerpo: un método
      declarado arriba usa campos declarados al final de la clase.
    - Las anotaciones no participan en la resolución. Un campo `@Autowired`
      resuelve por su **tipo declarado**, no por la anotación; `@Service` y
      compañía siguen siendo metadata de nodo, no señal de grafo.
    - `super.m()` resuelve contra la primera superclase si está en el fichero;
      `this.m()` y `m()` contra la clase actual; `Clase.m()` contra la clase si
      es local. `System.out.println` y `String.format` no generan nada.
    - `_java_bare_type()` limpia genéricos y arrays antes de buscar la clase:
      `Map<String,Object>` → `Map`, `com.example.Gateway[]` → `Gateway`.
    - **Divergencia de ramas documentada**: la rama regex no emite nodos
      `field`, así que un campo inyectado no resuelve sin tree-sitter. Es un
      edge omitido, nunca uno falso — las dos ramas coinciden en todo lo demás.
    - Sin cross-file todavía.
  - **Rust** (tree-sitter + fallback regex): los métodos viven en bloques
    `impl`, no en el `struct` — node id `modulo.Tipo.metodo`. Todos los `impl
    Tipo` de un fichero, incluido `impl Trait for Tipo`, aportan al mismo tipo.
    - Los tipos están **declarados** (parámetros, campos, anotaciones de
      `let`), así que no se infiere nada de un inicializador. El tipo de
      retorno solo se propaga si es exactamente `Self` o el propio tipo del
      `impl`: `-> Option<Self>` no vale, el valor es un `Option`.
    - Un `helper()` suelto **nunca** resuelve a un método. Rust no tiene
      receptor implícito: dentro de `impl Gateway`, `submit()` es una función
      libre, no `self.submit`. `_value_scopes` solo contiene módulos.
    - **Las macros no generan edges, ni las llamadas escritas dentro.**
      tree-sitter ve un cuerpo de macro como un token tree opaco, así que
      `assert_eq!(build(), 1)` no produce ningún `call_expression`. La rama
      regex blanquea esos token trees para no discrepar: sin eso las dos ramas
      diferían en **548 edges**, casi todos en módulos `#[cfg(test)]`.
    - `_rust_blank_noise()` distingue un literal de carácter de un lifetime:
      `&'a str` abre una comilla que nunca cierra. Solo es literal si la
      comilla de cierre llega en el ancho de un escape.
    - **Dos defectos preexistentes que destapó comparar las ramas**:
      `impl<'a> Walker<'a>` metía los genéricos en el node id, dejando
      `Walker<'a>.next` inalcanzable desde el struct; y
      `impl From<Vec<T>> for X` se leía como `impl From` porque `<[^>]*>` para
      en el primer `>`. Ambos arreglados — `_rust_impl_head()` escanea con
      conteo de profundidad en vez de con un grupo regex.
    - Para que las ramas emitan los mismos ids, el regex anida también bajo
      `mod` y bajo cuerpos de trait, y **descarta las `fn` anidadas dentro de
      otra `fn`** — tree-sitter no desciende a cuerpos de función, así que
      tampoco son símbolos allí. Antes el regex creaba un `unicode.imp`
      duplicado 29 veces en el mismo fichero.
    - Los ítems declarados dentro de un cuerpo de macro o de un comentario no
      son símbolos en ninguna rama.
    - Medido sobre 600 ficheros de crates.io: **589 con acuerdo exacto**,
      2522 edges tree-sitter vs 2518 regex. Las 26 discrepancias restantes
      salen todas de un mismo fichero de bindings COM autogenerados.
    - Sin cross-file todavía.
  - **Cross-file de Java**: cada `import` nombra una clase, así que la
    resolución es más directa que la de TypeScript (listas de símbolos) y más
    precisa que la de Go (paquete entero). El nodo import lleva
    `_java_import` (ruta sin la keyword `static`) y `_java_import_type`
    (`class`/`static`/`wildcard`/`static_wildcard`).
    - **El resolutor de Java corre ANTES que el de TypeScript.** El pase de TS
      es el barrendero: descarta todo edge pendiente cuyo nodo import no lleve
      `_source_module`. Puesto después, TS habría destruido cada edge de Java.
    - `_build_java_symbol_index()` indexa por FQN y por nombre corto; si el
      corto colisiona solo resuelve el FQN — que es justo lo que un `import`
      siempre proporciona. El FQN se reconstruye desde el **id del nodo
      relativo a su fichero**, no desde el label: si no, una clase anidada
      `Outer.Inner` colisiona con una `Inner` de primer nivel.
    - La ambigüedad se resuelve con **silencio**: un nombre alcanzable por dos
      `import pkg.*` no resuelve a ninguno.
    - El id de un wildcard es su ruta completa (`import_com_alpha_*`). Con el
      último segmento (`import_*`) dos wildcards del mismo fichero colapsaban
      en un nodo y se perdía un paquete entero sin aviso.
    - **Bug encontrado**: `_JAVA_IMPORT_RE` se comía la keyword en su propio
      grupo de captura, así que todo import estático se registraba como si
      fuera una clase. La keyword se conserva para clasificar y se quita del
      label, que antes divergía entre ramas.
    - La rama regex ya resuelve campos: no hacían falta nodos `field`, hacía
      falta el **tipo declarado**. Localiza los cuerpos de clase y lee como
      campo toda declaración que cae fuera de cualquier método. Con eso
      desaparece la divergencia de ramas que quedaba en Java.
    - Sin herencia cross-file ni resolución por paquete implícito (una clase
      del mismo paquete usada sin `import` no tiene dónde aparcar el edge).
    - Proyecto en capas de 16 ficheros estilo Spring: **5 → 30 calls (6×)**,
      25 de ellos cross-file. Las dos ramas dan 30.
  - **Cross-file de Rust**: los node ids siguen el árbol de **ficheros**, los
    `use` siguen el de **módulos**, y divergen en un punto:
    `src/payments/mod.rs` es el módulo `crate::payments`, no
    `crate::payments::mod`. `_rust_module_paths()` hace ese mapeo y detecta
    las raíces de crate por nombre de fichero (`lib.rs`/`main.rs`), así que en
    un workspace cada `crate::` apunta a la suya.
    - `_rust_expand_use()` expande un `use` a pares `[nombre local, ruta]`:
      grupos `{A, B}` anidados, alias `as`, `self` dentro de grupo, y `*`.
    - **Solo se siguen `crate::`, `super::` y `self::`.** Desde la edición 2018
      `use foo::bar` es *siempre* el crate externo `foo`, nunca un módulo
      local. Python sí resuelve rutas desnudas porque ahí sí pueden ser
      locales; aplicar esa regla en Rust inventaría edges que el compilador
      nunca hace.
    - La ambigüedad se agrupa por **(llamante, símbolo)**, no por nodo import:
      un nombre alcanzable por dos globs llega como dos edges pendientes y
      juzgar cada uno por separado resolvía los dos en vez de ninguno.
    - `_rust_file_modules()`: `pub mod helper;` emite también un nodo `module`
      desde el mismo fichero. El del propio fichero es el de **id más corto**,
      porque una declaración siempre añade un segmento. Sin esa regla el mapa
      se quedaba con el nodo equivocado y `self::`/`super::` fallaban en todo
      `mod.rs`.
    - Los `use` se anclan siempre al **fichero**, estén dentro de un `mod` o de
      un cuerpo de función. `_visit_fn` no desciende, así que tree-sitter
      perdía los `use` locales a función que el regex sí veía.
    - `_lookup_function()` acepta `struct`: `Pattern(&bytes)` construye un
      tuple struct, y llamar así a un struct no-tupla no compila, de modo que
      un nombre llamado que además es struct solo puede ser el constructor.
    - **Limitación**: el tipo de retorno de una función importada no se
      propaga. `let e = Engine::new(3); e.run()` resuelve `Engine::new` pero no
      `e.run()` — saberlo exige leer el fichero destino, una segunda ronda
      cross-file. Con anotación, parámetro o campo sí resuelve.
    - 14 crates reales de crates.io: 1930 → **2528 calls, 598 cross-file
      (1,31×)**. Divergencia solo-regex 22 sobre 2528.
  - **Cross-file de Go**: un paquete es un **directorio**, no un fichero.
    `auth/auth.go` y `auth/handlers.go` son un paquete pero emiten dos nodos
    módulo, así que `auth.Login()` busca en todos los ficheros del directorio.
    Java y Rust no necesitaban esto.
    - **Bug del parser de imports**: `_GO_IMPORT_PATH_RE` escaneaba solo lo
      entrecomillado, así que el alias explícito, el `.` y el `_` se perdían —
      y un `import _ "x"` con la misma ruta que otro import quedaba
      deduplicado y borrado. Ahora `_GO_IMPORT_SPEC_RE` captura el nombre
      previo y el nodo lleva `_go_import`, `_go_alias` y `_go_alias_explicit`.
    - **El alias automático NO es el último segmento de la ruta**, es el
      nombre **declarado** del paquete: el repo `toml-test` declara
      `package tomltest`. El alias adivinado desde la ruta se marca como tal y
      es el grafo quien confirma el nombre real al resolver.
    - **`package main` no es importable** y se excluye del índice. Sin eso,
      `cmd/toml-test/` (que declara `main`) secuestraba la clave `toml-test` y
      dejaba fuera al paquete raíz, que era el destino verdadero.
    - Se quita el sufijo de versión de módulo: `github.com/x/pkg/v2` vive en
      el directorio `pkg`.
    - **Solo los nombres exportados cruzan el límite de paquete.** Un símbolo
      en minúscula nunca resuelve, que es lo que impide a un dot import
      engancharse al helper privado de otro paquete.
    - La ambigüedad se agrupa por **(llamante, símbolo)**, igual que en Rust:
      dos dot imports que ofrecen el mismo nombre no resuelven a ninguno.
    - El emparejamiento ruta↔directorio es **por sufijo**, así que un paquete
      local llamado `redis` podría capturar un `import "github.com/go-redis/
      redis"`. Es la misma exposición que ya tienen Python y Rust con el
      sufijo, no una nueva.
    - El tipo de retorno se propaga dentro del fichero (ver más abajo); si la
      función constructora vive en otro paquete, aún no.
    - Proyecto Go real (toml-test, 13 ficheros): 78 → **83 calls, 5
      cross-file**.

- **Propagación del tipo de retorno (Go, Rust, Java, TypeScript)** — todos los
  nodos de función llevan `return_type` con el nombre limpio: `*Server` →
  `Server`, `Self` → el struct del `impl`, `Promise<Client>` → `Client`. El
  binding ocurre **dentro del fichero**, donde ya vive el mapa de locales, no en
  un pase de proyecto: así no hay que tocar los cuatro resolutores cross-file ni
  añadir claves privadas a los edges. La contrapartida es que una función
  constructora en **otro fichero** no propaga su tipo.
  - **Go desenvuelve `(T, error)`, Rust NO desenvuelve `Option<T>`.** No es una
    incoherencia: la tupla de Go se desestructura en la asignación, así que
    `srv` es realmente un `*Server`; el `Option<Engine>` de Rust es un valor
    envoltorio y `e.run()` sobre él no compila. Desenvolverlo inventaría edges
    que el compilador rechaza. El tipo interno sí se guarda como metadato.
  - TypeScript exige la anotación `: Type`. Un retorno inferido no vincula nada:
    rehacer la inferencia de TS estáticamente no es seguro.
  - Si una variable se vincula a **dos tipos distintos** en la misma función, se
    descarta en vez de quedarse con el último.
  - **Medición honesta: cero edges nuevos en proyectos reales.** PrismaStats
    488 → 488, toml-test 83 → 83. De las 114 funciones de PrismaStats con
    retorno anotado solo 12 devuelven un tipo del proyecto, y son interfaces de
    datos sobre las que nadie llama métodos. En 14 crates de Rust, 0 sitios
    recuperables: el idioma es `Engine::new()`, que ya funcionaba. La
    funcionalidad es correcta y está cubierta por 16 tests, pero su frecuencia
    real es mucho menor de lo que sugería su protagonismo en la documentación.

- **El servidor MCP expone cinco herramientas** (`slurp_query`, `slurp_explain`,
  `slurp_diff`, `slurp_suggest`, `slurp_impact`), todas `readOnlyHint` + `idempotentHint` para que el cliente
  pueda auto-aprobarlas.
  - `slurp_explain` usa **el mismo grafo que `slurp_query`**, con la
    comprobación de recarga incluida: las dos no pueden responder desde
    snapshots distintos en la misma sesión.
  - **El config del LLM se resuelve por llamada**, no al arrancar. Exportar una
    API key a mitad de sesión da prosa sin reiniciar, y si la resolución falla
    se cae al modo estructural en vez de dar error. Resolverlo al inicio habría
    convertido `no_llm` en un flag de lanzamiento.
  - Dos categorías de error, deliberadamente distintas: argumentos inválidos →
    JSON-RPC `-32602`; fallo de ejecución → `isError: true` con texto legible.
    Un fallo interno nunca mata el proceso.
  - **`find_node()` no falla con un nodo inexistente**: cae a la puntuación por
    relevancia y devuelve el mejor candidato. `slurp_explain` solo da `isError`
    con el grafo vacío. Es comportamiento preexistente, no un defecto nuevo.
  - `slurp_diff` es la única que lee ficheros nombrados por el llamante en vez
    del grafo cargado al inicio. Lleva `openWorldHint: false` porque solo toca
    disco local, no red — discutible, y marcado con un comentario.

- **`suggester.py` — la frontera del subgrafo.** Un subgrafo es un corte: lo que
  quedó justo fuera estaba lo bastante relacionado para ser vecino y no lo
  bastante para entrar. Esa frontera es lo más informativo que slurp sabe tras
  responder, y hasta v1.0.4 se descartaba.
  - Tres tipos de sugerencia, cada una solo si el grafo la respalda: **drill
    down** (el nodo más relevante tiene vecinos fuera), **sibling** (hay un
    clúster temático en la frontera), **risk** (un vecino con ≥6 dependientes).
  - **Cuatro defectos que solo aparecieron con grafos reales**, no con los de
    juguete: los nodos `import` inundaban los tokens (llevan el nombre de lo
    que importan → describen fontanería, no tema); `relevance_score` saturaba a
    1.0 porque los divisores estaban calibrados para grafos pequeños — ahora es
    la fracción de frontera cubierta; el drill-down casi nunca disparaba porque
    a budget realista los vecinos del nodo principal ya están dentro (ahora
    mira a 2 hops); y apuntaba a tests, que puntúan alto porque llevan el
    nombre de lo que prueban — pierden todo empate contra producción.
  - **El silencio es respuesta válida**: si el budget cubre la vecindad, la
    lista sale vacía en vez de rellenarse.
  - `slurp_suggest` re-ejecuta la query en lugar de recibir el subgrafo: MCP no
    tiene estado entre llamadas, y reenviarlo sería peor que recalcularlo.
  - **Acoplamiento a vigilar**: importa cuatro privados de `explainer.py`
    (`_RISK_HIGH_MIN`, `_display`, `_is_test`, `_split_callers`) siguiendo el
    precedente de `advisor.py` con `_tokenize`. Son ya cinco módulos
    compartiendo internos; esos cinco nombres formarían un `_graphutils.py`
    público sin romper nada.

- **`smart.py` — `--smart` produce siempre el grafo de un índice completo.** Hasta
  v1.0.6 reparseaba los ficheros cambiados sin volver a resolver: las llamadas
  cross-file acababan en nodos import, `_pending_symbol` se colaba en la salida, y
  como comparaba contra HEAD, un commit antes de reindexar daba "up to date" sobre
  un grafo viejo (`built_at_commit` era siempre `None`).
  - `slurp index` (e `init`) graba en `built_at_commit` el commit de HEAD. `--smart`
    compara contra ese commit (committed + staged + unstaged + untracked) y suma
    todo fichero con mtime ≥ el del grafo: cubre lo que se ensució al construir y
    luego se revirtió, y ficheros indexados que git ignora. Sin commit registrado
    (grafos anteriores a v1.0.7) o con uno que ya no existe → índice completo.
  - **Vía rápida solo si los nodos de los ficheros reparseados son idénticos**,
    imports incluidos, en todo salvo `source_location`. Los índices de resolución
    leen `id`/`type`/`label`/`source_file` y los de Rust y TS recorren también los
    imports; con los mismos nodos ningún otro fichero puede resolver distinto. Por
    eso no hay expansión a dependientes: se eliminó, reparseaba de más (editar
    `budget.py` arrastraba 3.209 nodos) y no garantizaba nada.
  - Altas, bajas, renombrados y cambios de símbolos o imports → índice completo,
    con el motivo impreso. Coste medido: ~0,1 s sobre un índice completo.
  - `index_project()` y la vía rápida comparten `_assemble_graph()` (dedup, ignore,
    filtro y los 5 resolutores): los edges ya resueltos pasan por los resolutores
    sin tocarse, así que alimentarlo con lo conservado + lo reparseado reproduce
    el índice completo. La resolución cuesta 6–13 ms; el coste está en parsear.
  - `TestSmartEquivalence` compara `--smart` con un índice completo en 13
    escenarios × con/sin commit, y exige qué camino toma cada uno. Un cambio en
    smart que no lo supere rompe la invariante.
  - Limitación: si cambia `.slurpignore` entre dos ejecuciones, `--smart` no lo
    detecta; hay que reindexar completo.

- **`impact.py` — el radio *antes* de editar.** `slurp diff` responde a
  posteriori comparando dos snapshots; `impact` responde antes, con el grafo
  que ya hay. Umbrales de riesgo replicados de `explainer._risk_level` (6/2)
  para que un fichero y sus nodos nunca discrepen sobre el mismo código.
  - **El nodo-fichero no puede ser el de mayor riesgo.** Acumula las
    importaciones del fichero, así que ganaba siempre — y nadie edita "el
    fichero", edita funciones. Filtrar por `type` **no sirve**: el grafo de
    graphify no trae `type` en absoluto (todos `None`). El rasgo que sí lo
    distingue es que su **label es el nombre del fichero**.
  - **Contenido ≠ seguro.** Un nodo llamado solo desde su propio fichero no
    entra en el radio externo, pero tampoco es SAFE TO EDIT. Son dos preguntas
    distintas y la primera versión las confundía: `recalcularPlayerStats()`
    salía como segura teniendo 3 llamadores.
  - **Sin call graph, el riesgo es `UNKNOWN`, nunca `LOW`.** En los 11 lenguajes
    sin edges `calls`, cero llamadores es un hueco del grafo: decía "SAFE TO EDIT"
    de todo fichero C#. `_graphutils._call_graph_gap()` es la única fuente: hay
    call graph si la extensión está en `_CALL_GRAPH_EXTENSIONS` (las 5 que extrae
    slurp) **o** si el grafo trae algún `calls` para esa extensión (graphify los
    extrae para cualquier lenguaje). Con hueco: `risk_level="UNKNOWN"` +
    `risk_reason`, `safe_nodes` vacío, y en `explain` el rol, la prosa y
    ARCHITECTURE dicen "unknown" en vez de "isolated"/"nothing"; el contexto del
    LLM lleva una NOTE para que no lo describa como código muerto.
  - Rutas por **sufijo normalizado**, con la coincidencia exacta ganando
    siempre, para que un `vendor/lib/core.py` no secuestre la consulta.
  - **Un fichero desconocido no es un error**: resultado vacío, exit 0 y un
    mensaje que sugiere re-indexar. Solo `--viz` falla, porque no hay qué
    dibujar.
  - El fixture de tests usa `file_path` y los grafos reales `source_file`;
    `_source_of` lee ambos. Un test que mire solo uno falla en silencio.

- **`slurp eval --retrieval` — calidad de recuperación sin LLM.** Usa los
  `relevant_nodes` que ya traían las preguntas (hasta v1.0.7 nadie los leía):
  recall = relevantes dentro del subgrafo / relevantes presentes en el grafo;
  MRR = 1 / rango del mejor relevante por score, desempate por id (determinista).
  Misma selección que `slurp QUERY` (`RETRIEVAL_MIN_SCORE`). Preguntas cuyos
  nodos no existen en el grafo se marcan no evaluables, no cuentan como fallo.
  Línea base PrismaStats (`slurp index`, budget 4000): recall 1,000 · MRR 0,400,
  15/15 preguntas iguales a la implementación del diagnóstico. Es la métrica para
  medir cualquier cambio de scorer o de presupuesto.
  - Cada pregunta reporta también `tokens_used`, la medida del texto que enviaría, y
    `--format markdown|json|yaml` elige en qué formato se presupuesta y se mide (el
    tamaño por nodo cambia mucho entre formatos). `--format` sin `--retrieval` se
    rechaza: el eval con LLM envía markdown.
  - La cabecera lleva `question.question`. Hasta v1.1.0 llevaba el `repr` del
    `EvalQuestion` entero (88 tokens en vez de 28 en auth-01); las cifras
    publicadas (0,867 a 4000, 1,000 a 16000) no cambiaron al corregirlo.

- **`_graphutils.py` — el hogar de lo compartido.** Cada uno de estos nombres
  fue un privado del módulo que lo necesitó primero, importado con underscore
  desde los demás según aparecían. A seis módulos de profundidad eso deja de
  ser un atajo. Viven aquí: `_tokenize`, `_display`, `_is_test`,
  `_split_callers`, `_box`, `_RISK_HIGH_MIN`/`_RISK_MEDIUM_MIN`, más
  `_label`, `_callers`, `_callees` y `_TEST_MARKERS`.
  - **Los tres últimos no estaban en la lista original pero tuvieron que
    moverse**: `_display` depende de `_label`, y `_split_callers` de
    `_callers` y `_TEST_MARKERS`. Dejarlos en `explainer` habría creado un
    import circular.
  - **`impact.py` duplicaba `_RISK_HIGH_MIN`** con un comentario que decía
    «mirrors explainer» — exactamente el tipo de copia que diverge en
    silencio. Ahora hay un solo 6.
  - Conservan el underscore: siguen siendo internos de slurp, solo que con un
    hogar en vez de prestados desde donde tocara.
  - Los módulos origen los reimportan, así que `from slurp.scorer import
    _tokenize` y `from slurp.explainer import _split_callers` siguen
    funcionando — los tests no hizo falta tocarlos.

---

## Principios de desarrollo

1. **Tests primero.** Cada módulo lleva sus tests. Mínimo: happy path + edge cases obvios + caso de grafo vacío.
2. **Un módulo, una responsabilidad.** No mezclar lógica de grafo con lógica de tokens ni con CLI.
3. **Fail loud, fail early.** Errores claros con mensajes accionables. Nunca fallar silenciosamente.
4. **Sin dependencias innecesarias.** Antes de añadir una librería, preguntarse si stdlib lo resuelve.
5. **Compatibilidad graphify primero.** `graph.json` de graphify es el formato de entrada principal. Todo cambio debe mantener compatibilidad con él.
6. **Documentar decisiones no obvias** con comentario `# DECISION:` en el código.
7. **Type hints en todas las funciones públicas.** Sin excepción.

---

## Estructura del proyecto

**Working directory:** la raíz del repo git es `~/Desktop/Slurp/slurp/` — un nivel
por debajo de la carpeta que abre el editor (`~/Desktop/Slurp/`). Todos los comandos
de este documento (`uv run pytest`, `ruff check`, `uv build`, `git`) se ejecutan
desde `~/Desktop/Slurp/slurp/`, que es donde vive este CLAUDE.md.

**Estado actual:** v1.1.0 · 2490 tests · `ruff check slurp/` limpio.

**⚠️ IMPORTANTE — `uv sync --extra X` desinstala los extras no mencionados.**
Sincroniza al conjunto exacto de extras que le pases, así que añadir uno con
`uv sync --extra llm-openai` a secas borra las 12 gramáticas de tree-sitter (los
tests pasan de 1772 a `1682 passed, 90 skipped`) y puede dejar huérfano el
entry-point `slurp` del `.venv`. Para añadir un extra sin romper los existentes,
usar **siempre** la forma completa:

```bash
uv sync --extra all-languages --extra llm-openai --reinstall-package slurp-graph
```

Señal de que ha pasado: la suite reporta tests *skipped* donde antes había 0, o
`uv run slurp` ejecuta una versión distinta de la del repo.

**Publicación en PyPI** (v1.1.0, #63): `.github/workflows/publish.yml`, trusted
publishing por OIDC, sin secrets. Se dispara con un push de tag `v*` o a mano
(`workflow_dispatch`) con un tag existente: el fichero del workflow sale de la rama
elegida (main) y el código del tag, así se publica un tag anterior al workflow
(v1.0.7). Job `build`: comprueba que tag, `pyproject.toml` y `__init__.py` coinciden,
`uv sync --locked --all-extras`, la suite, `uv build`, `dist/` como artefacto. Job
`publish`: `environment: pypi`, `id-token: write`, `pypa/gh-action-pypi-publish`.
Acciones fijadas por SHA con la versión en comentario; `tests/test_publish_workflow.py`
lo vigila (pins, OIDC solo en publish, sin secrets, sin `${{ }}` dentro de un `run:`).
Nunca publicar a mano desde `dist/` local: los ficheros de la 1.0.7 que hay ahí no se
tocan.

**Latencia reproducible** (v1.1.1, #67): `scripts/latency.py GRAPH --method
original|current|both --runs N [--src DIR]` — los dos métodos de README › Performance,
un proceso nuevo por ejecución, `--src` para medir otro árbol (`git archive vX slurp`).
No va en el paquete; `tests/test_latency_script.py` lo mantiene ejecutable.

```
~/Desktop/Slurp/slurp/         ← raíz del repo git — working directory
├── CLAUDE.md                  ← este fichero
├── .github/workflows/publish.yml ← publicación en PyPI por OIDC
├── README.md
├── USAGE.md
├── pyproject.toml
├── .slurpignore.example
├── slurp/
│   ├── __init__.py        ← versión y exports públicos
│   ├── cli.py             ← entrada CLI (click)
│   ├── loader.py          ← lee graph.json → nx.DiGraph
│   ├── scorer.py          ← puntúa nodos por relevancia
│   ├── budget.py          ← selecciona subgrafo óptimo por tokens
│   ├── formatter.py       ← serializa subgrafo a texto para LLM
│   ├── exporter.py        ← bloques de contexto Claude/ChatGPT/CLAUDE.md
│   ├── benchmark.py       ← mide ahorro real de tokens vs grafo completo
│   ├── audit.py           ← registra queries en .slurp/audit.jsonl
│   ├── diff.py            ← compara dos grafos y analiza impacto
│   ├── viz.py             ← genera HTML interactivo del subgrafo
│   ├── mcp.py             ← servidor MCP stdio (JSON-RPC 2.0)
│   ├── ignore.py          ← filtra nodos por .slurpignore
│   ├── injector.py        ← extrae código fuente real para --inject-code (v0.5.0)
│   ├── indexer.py         ← indexador estático autónomo (v0.6.0)
│   ├── smart.py           ← re-indexado incremental (--smart), equivalente a un índice completo
│   ├── advisor.py         ← recomienda budget óptimo desde el historial de queries
│   ├── federation.py      ← mergea varios grafos en uno (--graph repetido)
│   ├── explainer.py       ← explica un nodo en lenguaje natural (LLM + fallback estructural)
│   ├── evaluator.py       ← benchmark de calidad con LLM judge (slurp eval)
│   ├── suggester.py       ← propone queries de seguimiento desde la frontera del subgrafo
│   ├── impact.py          ← radio de impacto de editar un fichero, sin segundo snapshot
│   └── _graphutils.py     ← utilidades compartidas entre módulos que leen el grafo
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_loader.py
    ├── test_scorer.py
    ├── test_budget.py
    ├── test_formatter.py
    ├── test_exporter.py
    ├── test_benchmark.py
    ├── test_audit.py
    ├── test_diff.py
    ├── test_ignore.py
    ├── test_mcp.py
    ├── test_cli.py
    ├── test_injector.py
    ├── test_indexer.py
    ├── test_smart.py
    ├── test_advisor.py
    ├── test_federation.py
    ├── test_explainer.py
    ├── test_evaluator.py
    ├── test_suggester.py
    └── test_impact.py
```

---

## Arquitectura de módulos

### `slurp/loader.py`

Lee `graph.json` en memoria como `nx.DiGraph`.

**Formato graphify soportado:**
```json
{
  "nodes": [
    {
      "id": "node_id",
      "label": "Human readable label",
      "type": "function|class|module|concept|...",
      "description": "What this node represents",
      "importance": 7,
      "file_path": "src/auth.py"
    }
  ],
  "links": [
    {
      "source": "node_id_a",
      "target": "node_id_b",
      "relation": "calls|imports|implements|...",
      "weight": 2
    }
  ]
}
```

**Nota:** graphify usa `links` como key para edges (no `edges`). El loader soporta ambos, con `links` teniendo prioridad. Los nodos de graphify usan `file_type` y `source_file` en lugar de `type` y `file_path`.

**API pública:**
```python
def load_graph(path: Path) -> nx.DiGraph:
    """Carga graph.json y retorna un DiGraph. Lanza SlurpLoadError si falla."""

def detect_format(data: dict) -> str:
    """Retorna 'graphify' o 'generic'."""
```

**Errores:**
- `SlurpLoadError` si el archivo no existe, no es JSON válido, o no tiene nodos.

---

### `slurp/scorer.py`

Dado el grafo y una query string, puntúa cada nodo por relevancia.

**Señales combinadas:**
- **Structural (40%):** PageRank implementado con power-iteration puro (sin numpy)
- **Semantic (60%):** TF-IDF manual con tokenización mejorada: separa camelCase, snake_case, normaliza a minúsculas
- **Boost +0.3** para nodos con `file_type == "code"` (clamped a 1.0)

**Score final:** `score = min(1.0, 0.4 * pagerank_normalized + 0.6 * tfidf_similarity + code_boost)`

**Decisiones de implementación:**
- `_pagerank()` — power-iteration puro, ~20 líneas, sin numpy. Converge en O(iter × edges)
- `_tokenize()` — inserta espacios en límites camelCase/PascalCase, extrae runs alfanuméricos, añade forma compuesta original
- `_tfidf_scores()` — IDF suavizado sklearn-style, similitud coseno
- `lru_cache` en tiktoken para evitar carga repetida del vocab

**API pública:**
```python
def score_nodes(G: nx.DiGraph, query: str) -> dict[str, float]:
    """Retorna dict {node_id: score} con scores normalizados entre 0 y 1."""

def score_nodes_detailed(G: nx.DiGraph, query: str) -> dict[str, tuple[float, float, float]]:
    """Retorna dict {node_id: (final, pagerank_norm, tfidf)} para --explain."""
```

---

### `slurp/budget.py`

**El presupuesto acota el texto que se envía, medido** (v1.1.0). Hasta v1.0.7
contaba un resumen por nodo (`_serialize_node`, eliminado) y nunca las
relaciones: `--budget 8000` daba 19k–42k tokens reales.

**`build_context()` → `(texto, subgrafo, stats)`**, con `count_tokens(texto) ≤ budget`:
1. **Suelo**: la salida mínima (cabecera + aviso de presupuesto) tiene que caber;
   si no, `BudgetTooSmallError` (subclase de `ValueError`) con el mínimo. CLI →
   `ClickException`; MCP → JSON-RPC `-32602`.
2. **Greedy en una sola cola por valor** para nodos y relaciones. Nodo = score
   efectivo (boost a vecinos `× neighbor_decay` como antes). Relación = elegible
   cuando sus dos extremos están dentro, valor `min(extremos) × peso`; peso 0,5
   para `contains`/`imports_from`/`imports`/`defines` (lo implica la ruta y los
   imports), 1,0 para el resto. Empate: nodo antes que relación, luego por id.
   Cada elemento se costea con **las mismas funciones del formatter**
   (`formatter.item_text`, con un score de relleno de anchura máxima → coste
   independiente de la query y cacheado en `_item_cost_cache`). Se apunta a
   `(budget − cabecera) × 0,98`.
3. **`fit_to_budget()` es la garantía**: renderiza el texto completo, lo cuenta y,
   mientras se pase, quita los elementos de menor valor (relación antes que nodo,
   un nodo arrastra sus relaciones). La suma de piezas NO es la garantía: los
   límites entre piezas cambian la tokenización.
4. **Las stats dicen la verdad**: `tokens_used` es la medida del texto final, que
   la contiene → punto fijo (se rellena y se vuelve a medir).
5. **Nunca vacío en silencio**: si no cabe ni un nodo (o el recorte los quita
   todos, p. ej. tras `--inject-code`), la salida es la cabecera con un aviso y
   `stats["min_budget"]` = lo que necesita el nodo de más valor, medido.
6. **Por qué falta cada nodo** (v1.1.0, #56): las stats llevan `nodes_over_budget`
   (puntuaron ≥ `min_score` y no cupieron), `nodes_below_min_score` y `min_score`;
   `selected + over + below = total`. Las calcula `_measure` con `left_out=(candidatos,
   min_score)`, así que siguen siendo ciertas tras cada recorte; quien llame a
   `fit_to_budget` por su cuenta (el `--inject-code` del CLI) tiene que pasar
   `left_out` o las pierde. El tip imprime una línea por causa y nunca dice
   "connected" (antes: "1977 additional connected nodes — increase --budget" en
   PrismaStats graphify, 672 de ellos bajo min_score). Stats hechas a mano, sin el
   desglose, reciben un recuento neutro sin causa.
- `select_subgraph()` devuelve `(subgrafo, stats)` del mismo cálculo; hay que
  pasar el mismo `format` y `query` a `format_subgraph` para reproducir el texto.
- `render=` permite medir un envoltorio (lo usa `slurp export`).
- El subgrafo se construye con `_assemble()` en orden fijo: `G.subgraph()` itera un
  `set` cuando la selección es pequeña y el orden de las relaciones cambiaba entre
  procesos (aleatorización de hashes).
- Coste medido: +3–7 ms de mediana por query en caliente (PrismaStats, slurp).
- **Presupuesto por defecto: 4000 en CLI y MCP, revisado en v1.1.0 (#62) y sin
  cambios.** `slurp eval --retrieval`, PrismaStats `slurp index` (15 preguntas) y slurp
  (16): en markdown el recall es plano de 4000 a 8000 (0,867 / 0,688); 12000 da 1,000
  en PrismaStats con el triple de tokens. En JSON 4000 baja a 0,656 / 0,562 y 6000 lo
  iguala: un nodo cuesta ~2× en JSON. El MCP siempre envía markdown (`slurp_query` no
  tiene formato). Decisión del autor: 4000 + nota en el README para json/yaml (~6000),
  sin default dependiente del formato. Tabla en README › Choosing a budget.
- **Factor de tokens** (`token_factor`, `--token-factor`, `$SLURP_TOKEN_FACTOR`, por
  defecto 1,0, rango 0,1–10): multiplica toda medida (piezas, cabecera, texto final),
  redondeando hacia arriba. Medido aquí contra `messages.count_tokens` de Anthropic:
  ~1,56× cl100k_base en la salida de slurp — una estimación, no una garantía. El MCP lo
  lee por llamada. Con 1,0 la salida es byte a byte la de siempre (la clave
  `token_factor` solo aparece en stats si ≠ 1,0).

### `slurp/formatter.py`

Serializa un subgrafo a texto estructurado listo para inyectar en un prompt.

**Formatos:** `markdown` (default), `json`, `yaml` (serializer propio, sin PyYAML)

**API pública:**
```python
def format_subgraph(G: nx.DiGraph, stats: dict, format: str = "markdown") -> str:
    """Serializa el subgrafo al formato especificado."""
```

---

### `slurp/audit.py`

Registra cada query en `.slurp/audit.jsonl` (append-only).

**API pública:**
```python
def log_query(query, graph_path, stats, top_nodes, audit_dir=Path(".slurp")) -> None
def read_audit(audit_dir=Path(".slurp")) -> list[dict]
def top_nodes_from_audit(audit_dir=Path(".slurp"), n=10) -> list[tuple]
```

**Ahorro: el método del benchmark o nada** (v1.1.0, #57). Ahorro = `(tokens_full −
tokens_used) / tokens_full`, con `tokens_full` = el grafo entero impreso igual
(`budget.full_graph_tokens`). Solo `slurp benchmark` y el badge de `--viz` lo miden.
`slurp audit`, `slurp session` (y `session.log`) y `slurp advisor` no tienen ese dato y
**no muestran ahorro**: medirlo cuesta 70–700 ms por consulta frente a 4–7 ms de la
consulta (PrismaStats y slurp, markdown y JSON). Hasta v1.0.7 los tres lo inventaban
con un coste fijo por nodo, igual que el viz de `slurp diff` sus contadores de tokens
(ahora 0 → el viz no los pinta). `TestOneSavingsMethod` (test_benchmark) falla si
reaparece cualquier forma de coste fijo por nodo en `slurp/*.py`.

---

### `slurp/diff.py`

Compara dos versiones de graph.json y analiza impacto de cambios.

**API pública:**
```python
def diff_graphs(old: nx.DiGraph, new: nx.DiGraph) -> GraphDiff
def affected_subgraph(G: nx.DiGraph, diff: GraphDiff, hops: int = 2) -> nx.DiGraph
def format_diff(diff: GraphDiff, G_new: nx.DiGraph) -> str
def build_diff_viz_graph(G_old, G_new, diff, hops) -> nx.DiGraph
```

**`GraphDiff` dataclass:**
- `added_nodes`, `removed_nodes`, `modified_nodes` — listas de node IDs
- `added_edges`, `removed_edges` — listas de (source, target)
- `impact_score` — `1 − e^(−D/k)`, D = llamadores de producción distintos de los nodos borrados
  (grafo viejo) y de los nodos cuyo label/tipo/llamadas salientes cambiaron (grafo nuevo),
  sin tests ni `contains`. k y las bandas se **derivan** de `_RISK_HIGH_MIN`/`_RISK_MEDIUM_MIN`
  (leídas al llamar): diff, impact y explain dan la misma banda para el mismo D — lo fija
  `TestBandsDerivedFromRiskConstants`, también cambiando las constantes. Hasta v1.0.7 era la
  centralidad media (máx. ~0,03): borrar createAdminClient (97 llamadores) daba 0,0012 "low".
  `dependents`, `unknown_nodes`, `risk_reason`: sin call graph, un "low" pasa a "unknown"
  (`diff_severity`). Titular y color del panel del CLI salen de la misma banda.
- `removed_neighbors` (v1.1.1, #58) — vecinos directos de cada nodo borrado **en el grafo
  viejo**, que es donde existe, filtrados a los que siguen en el nuevo. Entran en el
  "risk neighborhood" de `format_diff` (y del `slurp_diff` del MCP), y el viz dibuja el
  nodo borrado con sus aristas antiguas en vez de aislado. Antes se buscaba en el grafo
  nuevo: borrar una función con 97 llamadores no listaba ninguno en riesgo. El rename de
  `select_subgraph` del README pasó de 2 a 17 nodos en riesgo.
- `has_changes` — property bool

**Colores en viz:** verde=added, rojo=removed, amarillo=modified, gris=unchanged

---

### `slurp/viz.py`

Genera HTML interactivo con vis.js para visualizar subgrafos.

**vis-network 9.1.9 va vendorizado e inline** (`slurp/_vendor/`, con sus licencias
Apache-2.0/MIT): el HTML funciona sin red y no ejecuta lo que sirva un CDN. Es
`dist/vis-network.min.js` del tarball de npm, idéntico byte a byte al de cdnjs que
usaba la v1.0.5; un test fija su sha256. Coste: +476.620 bytes por página (~31 KB →
~508 KB a 50 nodos). Todo dato embebido pasa por `_script_json()`, que escribe cada
`<` como `\u003c`: un `</script>` en una descripción cerraba el script.

**API pública:**
```python
def build_html(
    G: nx.DiGraph,
    stats: dict,
    scores: dict[str, float],
    query: str,
    max_nodes: int = 50,
    mode: str = "normal",  # "normal" | "diff"
) -> str
```

**Features:**
- Nodos coloreados por tipo, tamaño proporcional a score
- Panel lateral con label, tipo, score, descripción, archivo, conexiones
- Header con query, nodes selected/total, tokens used/budget, coverage%, savings badge
- Física: estabilización 500 iteraciones y freeze en `stabilizationIterationsDone`
  (no en `stabilized`: con 745 nodos el reposo natural no llegaba en 30 s y el hilo
  principal ejecutaba 29 s de script; ahora congela a los 3,3 s)
- Nodos aislados con opacity 0.4
- **Badge de ahorro solo con cifras medidas** (v1.1.0): `stats["tokens_full"]` = el grafo
  entero impreso con el mismo formatter y query (`budget.full_graph_tokens`). Sin esa
  cifra no hay badge, y sin presupuesto (impact, diff) tampoco contador de tokens. Antes
  asumía 50 tokens/nodo y `impact --viz` proclamaba ~100% de ahorro.
- Modo diff: verde/rojo/amarillo/gris, tamaños compactos (12-28px), font 11px

---

### `slurp/mcp.py`

Servidor MCP stdio, protocolo JSON-RPC 2.0, stdlib pura.

**Herramienta expuesta:** `slurp_query(query: str, budget: int = 4000) -> str`

**Métodos soportados:** `initialize`, `ping`, `tools/list`, `tools/call`

**Uso:**
```bash
slurp serve --graph graph.json
```

**Configuración `.mcp.json`:**
```json
{
  "mcpServers": {
    "slurp": {
      "command": "/path/to/.venv/bin/slurp",
      "args": ["serve", "--graph", "/path/to/graphify-out/graph.json"]
    }
  }
}
```

---

### `slurp/ignore.py`

Filtra nodos del grafo según `.slurpignore`.

**Reglas soportadas:**
- `type:document` — excluye por file_type
- `file:*.md` — excluye por source_file (glob)
- `id:prefix_*` — excluye por node ID (glob)

**API pública:**
```python
def load_ignore(path: Path) -> SlurpIgnore
def apply_ignore(G: nx.DiGraph, ignore: SlurpIgnore) -> nx.DiGraph
```

---

## CLI — Comandos completos

```bash
# Setup guiado — primer comando en un proyecto nuevo (v0.6.0)
slurp init
slurp init --yes    # acepta todos los prompts (CI / scripting)

# Query principal
slurp "auth flow" --graph graph.json --budget 4000
slurp "auth flow" --graph graph.json --budget 4000 --format json|yaml
slurp "auth flow" --graph graph.json --budget 4000 --explain
slurp "auth flow" --graph graph.json --budget 4000 --viz
slurp "auth flow" --graph graph.json --budget 4000 --min-score 0.15
slurp "auth flow" --graph graph.json --budget 4000 --neighbor-decay 0.7
slurp "auth flow" --graph graph.json --budget 4000 --no-audit
slurp "auth flow" --graph graph.json --budget 4000 --ignore-file .slurpignore

# Estadísticas
slurp stats --graph graph.json

# Historial
slurp audit
slurp audit --top-nodes 20
slurp audit --audit-dir .slurp

# Diff entre versiones
slurp diff old.json new.json
slurp diff old.json new.json --viz
slurp diff old.json new.json --hops 2
slurp diff old.json new.json --budget 4000

# Servidor MCP
slurp serve --graph graph.json
slurp serve --graph graph.json --no-log    # desactiva el session log

# Session log del servidor MCP (v0.6.4)
slurp session
slurp session --last 10
slurp session --tail
slurp session --log-dir .slurp

# Export (v0.3.0)
slurp export "auth flow" --graph graph.json --budget 4000 --format claude|chatgpt|claudemd
slurp export "auth flow" --graph graph.json --budget 4000 --output context.md

# Indexar proyecto sin graphify (v0.6.0)
slurp index .
slurp index /path/to/project --output graph.json
slurp index . --watch
slurp index . --force    # reemplaza un grafo que no escribió slurp (graphify)
```

**`slurp index` nunca pisa en silencio un grafo ajeno.** La salida por defecto,
`graphify-out/graph.json`, es también donde graphify escribe el suyo — que lleva
relaciones extraídas por LLM que slurp no puede regenerar. `_is_foreign_graph()`
lo reconoce por la forma de primer nivel: slurp escribe exactamente
`{nodes, links, built_at_commit}`; graphify añade `directed`/`multigraph`/`graph`/
`hyperedges` (node-link de networkx). **`built_at_commit` no sirve para distinguirlos:
graphify también lo escribe.** Un fichero que no parsea cuenta como ajeno. Sin
`--force` se pregunta con default *no* (sin TTY lee EOF y aborta); `--smart` sobre un
grafo ajeno hace un índice completo en vez de fusionar; `slurp init` pregunta lo mismo
si el usuario rechaza reutilizarlo. Un grafo genérico `{nodes, links}` no se distingue
del de slurp y se trata como propio.

**Auto-discovery de graph.json** (sin --graph):
1. `./graph.json`
2. `./graphify-out/graph.json`
3. `./.graphify/graph.json`

### `slurp init` — setup guiado

Punto de entrada para un proyecto nuevo. No requiere flags. Secuencia:

1. **Detecta el lenguaje dominante** contando ficheros indexables por extensión.
   Reutiliza `iter_source_files()` del indexer, así que `node_modules/`, `.venv/`
   y demás directorios vendorizados no falsean el conteo. Empates por orden
   alfabético para que la salida sea determinista.
2. **Muestra un panel con el plan y pide confirmación.** Nada se escribe antes.
3. **Indexa** a `graphify-out/graph.json` con progress bar real, alimentado por
   el callback `on_file` de `index_project()`. Si ya existe un grafo
   (auto-discovery), ofrece reutilizarlo en lugar de re-indexar.
4. **Escribe `.mcp.json`** con la ruta absoluta del binario, detectada vía
   `shutil.which("slurp")` y clasificada como `uv tool install` o `pip install`.
5. **Resumen final** con nodos, edges, ruta del grafo, el JSON generado y la
   instrucción de reiniciar el asistente.

| Flag | Default | Descripción |
|---|---|---|
| `--yes`, `-y` | off | Acepta todos los prompts. Para CI y scripting. |

**Invariantes que no se deben romper:**
- `.mcp.json` existente **nunca se machaca a ciegas**: se pregunta primero y los
  demás MCP servers del fichero se preservan. Solo se reemplaza la entrada `slurp`.
- El progress bar se desactiva fuera de TTY para no meter ANSI en pipes ni en
  las capturas de CliRunner.
- La ruta del binario en `.mcp.json` es absoluta: los clientes MCP lanzan el
  servidor sin shell de login y un `slurp` pelado dependería del PATH heredado.
- `.slurp/` al `.gitignore` con `_ensure_gitignored`: solo si el fichero existe, sin
  duplicar, respetando cualquier regla previa (también `!.slurp/`). **`slurp config set`
  aplica las mismas reglas** (v1.1.1, #45) tras cada escritura de `config.json`, con
  cualquier clave, y solo si el directorio de config se llama `.slurp` y está bajo el
  directorio actual (`_gitignore_config_dir`): si no, la línea no lo cubriría.

---

## Roadmap de versiones

### v0.1.0 — Core MVP ✅
- [x] `loader.py` con tests
- [x] `scorer.py` con tests
- [x] `budget.py` con tests
- [x] `formatter.py` con tests
- [x] `audit.py` con tests
- [x] `cli.py` — comando `slurp` principal + `slurp stats`
- [x] `conftest.py` con fixture de grafo de ejemplo realista
- [x] README con ejemplos

### v0.2.0 — Usabilidad ✅
- [x] `slurp audit` con historial y top-nodes
- [x] `--explain` flag con desglose de scores
- [x] `.slurpignore` para excluir nodos por tipo o path
- [x] Auto-discovery de graph.json
- [x] Output JSON y YAML
- [x] `--viz` flag con visualizador interactivo HTML
- [x] Badge de ahorro de tokens en viz
- [x] `--min-score` para filtrar nodos irrelevantes
- [x] Scorer mejorado: camelCase, snake_case, boost file_type=code

### v0.3.0 — Integraciones ✅
- [x] `slurp serve` — servidor MCP stdio JSON-RPC 2.0
- [x] `slurp diff old.json new.json` — impacto de cambios con viz
- [x] `slurp export` — system prompt listo para Claude/ChatGPT/CLAUDE.md
- [x] Publicación en PyPI como `slurp-graph`

### v0.4.0 — Integraciones ✅
- [x] Embeddings opcionales vía `--backend embeddings` (anthropic o openai)
- [x] `slurp benchmark` — mide token savings vs inyectar grafo completo con métricas reales
- [x] Compatibilidad con otros formatos de grafo (neo4j export, GraphML)

### v0.5.0 — Integraciones ✅
- [x] `--inject-code` flag — lee el `file_path` de cada nodo seleccionado,
      localiza la función por `source_location` (línea), extrae el bloque
      de código real y lo añade debajo del nodo en el output markdown
- [x] Soporta Python, TypeScript/TSX, JavaScript, Go como parsers iniciales
- [x] `--inject-code` solo disponible cuando el subgrafo tiene ≤30 nodos
      para evitar explosión de tokens
- [x] Combina contexto semántico del grafo + código real en un único output
      listo para inyectar en Claude Code

### v0.6.0 — Autonomous Indexing ✅
- [x] `slurp index .` — genera graph.json propio sin depender de graphify
- [x] Soporte Python (stdlib ast), TypeScript/JS (tree-sitter o regex), Go (regex)
- [x] Java, Rust, C# y Ruby (tree-sitter o regex) con metadatos por lenguaje
- [x] Mismo formato graph.json que graphify — compatible con todo lo existente
- [x] `slurp index . --watch` — modo watch que re-indexa archivos modificados
- [x] `.slurpignore` aplicado también al indexador
- [x] Sin dependencias de LLM para indexar — análisis estático puro

---

## Decisiones de implementación relevantes

| Decisión | Motivo |
|---|---|
| Power-iteration PageRank sin numpy | Evita dependencia pesada, mismo algoritmo |
| TF-IDF sin sklearn | Mantiene deps mínimas, suficiente para v1 |
| YAML serializer propio | Sin PyYAML, stdlib pura |
| `lru_cache` en tiktoken | Evita recarga de vocab en cada nodo |
| `links` tiene prioridad sobre `edges` | Compatibilidad con formato real de graphify |
| Boost +0.3 para file_type=code | Docs compiten injustamente con código en queries técnicas |
| `DEFAULT_MIN_SCORE = 0.15` en todas partes (CLI, MCP, benchmark, eval, diff, librería) | Una medida describe lo que recibe el usuario. En grafos de `slurp index` no filtra (boost +0,3 a todo nodo código); hacerlo filtrar bajó el recall de 1,000 a 0,733 en PrismaStats — medido en v1.1.0 #13 — así que el boost no se toca |
| El presupuesto mide el texto final | La suma de piezas no coincide con el texto; solo medir garantiza ≤ budget |
| Freeze física al acabar la pasada de estabilización | Nodos quietos = grafo legible, y sin CPU ocupada sin fin en grafos medianos |
| `max_nodes=50` en viz | Mantiene visualizador legible independiente del budget |
| vis-network vendorizado inline | Viz offline y sin código de un CDN; +476 KB por HTML |

---

## Convenciones de código

- Type hints en todas las funciones públicas
- Docstrings en Google style
- Código y comentarios en inglés
- Commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- `ruff check .` debe pasar sin errores antes de cada commit
- `uv run pytest tests/ -q` debe pasar antes de cada commit
- Ambos se ejecutan desde `~/Desktop/Slurp/slurp/`, no desde la carpeta contenedora

---

## Lo que NO hacer

- ❌ No crear formato de grafo propio rompiendo compatibilidad con graphify — graphify es ahora opcional, pero el formato `{nodes, links}` debe seguir siendo el mismo
- ❌ No usar async — el CLI es síncrono
- ❌ No añadir UI web propia — slurp es capa de query, no visualizador standalone
- ❌ No over-engineer el scorer

---

## Cómo trabajar con Claude Code

Al inicio de cada sesión, Claude Code debe:

1. Leer este CLAUDE.md completo antes de escribir una sola línea de código.
2. Situarse en `~/Desktop/Slurp/slurp/` — es la raíz del repo. Lanzado desde
   `~/Desktop/Slurp/`, `pytest tests/` falla con "file or directory not found".
3. Ejecutar `uv run pytest tests/ -q` para ver el estado actual.
4. Preguntar en qué módulo o feature trabajamos hoy si no está claro.
5. Ejecutar `uv run pytest tests/ -q` después de cada módulo implementado.
6. No pasar al siguiente módulo hasta que los tests del actual pasen.
7. Hacer `ruff check slurp/` antes de cada commit.

## Git commits
NUNCA añadir "Co-authored-by: Claude" ni firmas 
similares en los mensajes de commit.