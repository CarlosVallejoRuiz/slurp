# Puerta de preguntas (fase 2, paso 2): jsoup y caddy, 2026-11-26

**Cómo se generaron.**

- Con el generador congelado (`questions.py`, commit `cfeaddc`) y la semilla del estudio (12).
- Son las 10 primeras preguntas aceptadas de cada proyecto en el orden del estudio, así que
  también son preguntas del estudio.
- Etapa B: jsoup aceptó 10 de 12 (descartadas: 1 por solo comentarios y 1 por más de 8
  definiciones); caddy aceptó 10 de 10.

**Criterios de la revisión** (diseño §2.5):

- (a) alguien que trabaja en el proyecto podría hacer o buscar esta pregunta para localizar el
  código, sin ver el diff;
- (b) los nodos incluyen la definición principal que cambia, con como mucho uno de más.

**Resultado: (a) 19/20, (b) 20/20, las dos 19/20 (95 %).** El umbral es el 80 %, así que **la
puerta pasa**.

| # | Commit | Pregunta | Nodos relevantes | (a) | (b) |
|---|---|---|---|---|---|
| 1 | jsoup 29bc6f50 | Setters for Tag name and namespace | clase `Tag` (campos que dejan de ser `final`, setters nuevos) | sí | sí |
| 2 | jsoup 922ee011 | Clone the treebuilder so that a Parser can be reused without stomping the streaming parse | `StreamParser` y su constructor | sí | sí |
| 3 | jsoup 5d31cac5 | Don't try to get child nodes if there aren't any yet | `Document.ensureXmlDecl`, clase `Document` (javadoc) | sí | sí, uno de más |
| 4 | jsoup a939fca5 | Prevent calling clear() on shared EmptyNodeList object | `Element.empty` | sí | sí |
| 5 | jsoup a908dc9c | Clean up varargs compilation warning | `Wikipedia.log` (ejemplo) | **no**: es un aviso del compilador; nadie lo buscaría | sí |
| 6 | jsoup c347db4c | Escape `<` and `>` when serializing attribute values | `Entities.appendEscaped`, `appendLt`, `Entities` | sí | sí |
| 7 | jsoup 3a767deb | Refactor `classNames` scanner and add `classList` | `Element`, `Element.classNames`, `Element.hasClass` | sí | sí |
| 8 | jsoup 6bef9383 | Fix parsing of SVG foreignObject in paragraphs | `HtmlTreeBuilder` (lista estática de la clase) | sí | sí |
| 9 | jsoup 01c823e3 | Make Parser instances threadsafe | 7 nodos de `Document`, `Node` y `Parser` | sí | sí |
| 10 | jsoup 63023f8b | Added `Elements.deselectAll()`, like `clear()` but no DOM impact | clase `Elements` (método nuevo) | sí | sí |
| 11 | caddy 362f33da | fileserver: New --precompressed flag | `command.init`, `cmdFileServer` | sí | sí |
| 12 | caddy 0e392c89 | core: Retry privileged binds on EACCES instead of checking port < 1024 | `WithBindCapability`, `isPrivilegedPort` | sí | sí |
| 13 | caddy 9eabd443 | cmd: Add --json flag to list-modules command | `cmdListModules`, `commands.init` | sí | sí |
| 14 | caddy 2ab043b8 | reverseproxy: query escape request urls when proxy protocol is enabled | `Handler.directRequest` | sí | sí |
| 15 | caddy 4f504588 | tls: expand placeholders in dns_challenge override_domain | `ACMEIssuer.Provision` | sí | sí |
| 16 | caddy 6727b2f3 | templates: Delete file headers on any template error | `Templates.executeTemplate`, `TemplateContext.funcHTTPError` | sí | sí |
| 17 | caddy 6d73d85c | caddyfile: prevent adding trailing space on line before env variable | `formatter.Format` | sí | sí |
| 18 | caddy 9f714839 | log: default logger should respect `{in,ex}clude` | `Logging.setupNewDefault` | sí | sí |
| 19 | caddy fcba554d | caddyhttp: New expected_underscore_headers server option | 6 nodos de `serveroptions`, `App.Provision` y `Server` | sí | sí |
| 20 | caddy 9283770f | reverseproxy: ignore duplicate collector registration error | `initReverseProxyMetrics`, `newMetricsUpstreamsHealthyUpdater` | sí | sí |

`nombra_símbolo`: 5 de 10 en jsoup y 1 de 10 en caddy.
