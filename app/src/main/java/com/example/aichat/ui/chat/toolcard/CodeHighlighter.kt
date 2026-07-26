package com.example.aichat.ui.chat.toolcard

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily

/**
 * Lightweight syntax highlighter for short code blocks embedded in chat.
 *
 * Zero dependencies. Tokenises code with a small set of regexes per language
 * and emits an [AnnotatedString] with coloured spans. Coverage is intentionally
 * "good enough" — keyword/string/comment/number — not a full grammar. Inspired
 * by Kimi's chat code-block rendering.
 *
 * WHY hand-rolled: the only mature Compose option (mikepenz multiplatform-
 * markdown-renderer) is not mirrored on Aliyun, and commonmark-java pulls
 * ~300 KB. MarkdownText.kt already uses this approach; this file extends it
 * to code blocks.
 */
object CodeHighlighter {

    /** Default palette — Kimi-ish dark syntax colours. */
    object Palette {
        val Keyword = Color(0xFFC792EA)   // purple
        val String = Color(0xFFC3E88D)    // green
        val Comment = Color(0xFF697098)   // grey
        val Number = Color(0xFFF78C6C)    // orange
        val Function = Color(0xFF82AAFF)  // blue
        val Tag = Color(0xFF89DDFF)       // cyan
        val Attr = Color(0xFFFFCB6B)      // yellow
    }

    /**
     * Highlights [code] for the given [lang]. Falls back to no-highlight
     * (plain monospace) when the language isn't recognised — keeps the
     * bubble readable without inventing wrong colours.
     */
    fun highlight(code: String, lang: String?): AnnotatedString {
        if (code.isEmpty() || lang.isNullOrEmpty()) return plain(code)
        return when (lang.lowercase()) {
            "python", "py" -> highlightGeneric(code, Keywords.PYTHON, "#", tripleQuotes = true)
            "javascript", "js" -> highlightGeneric(code, Keywords.JS, commentSingle = "//", commentMulti = "/*" to "*/")
            "typescript", "ts" -> highlightGeneric(code, Keywords.TS, commentSingle = "//", commentMulti = "/*" to "*/")
            "kotlin", "kt" -> highlightGeneric(code, Keywords.KOTLIN, commentSingle = "//", commentMulti = "/*" to "*/")
            "java" -> highlightGeneric(code, Keywords.JAVA, commentSingle = "//", commentMulti = "/*" to "*/")
            "go", "golang" -> highlightGeneric(code, Keywords.GO, commentSingle = "//", commentMulti = "/*" to "*/")
            "rust", "rs" -> highlightGeneric(code, Keywords.RUST, commentSingle = "//", commentMulti = "/*" to "*/")
            "sql" -> highlightSql(code)
            "bash", "sh", "shell", "zsh" -> highlightGeneric(code, Keywords.BASH, "#")
            "json" -> highlightJson(code)
            "html", "xml" -> highlightMarkup(code)
            "css" -> highlightCss(code)
            "yaml", "yml" -> highlightYaml(code)
            else -> plain(code)
        }
    }

    private fun plain(code: String): AnnotatedString = buildAnnotatedString {
        append(code)
    }

    /**
     * Generic C-family / Python-ish highlighter. Renders in this order:
     *   1. comments (overrides everything below)
     *   2. strings  (overrides keywords/numbers)
     *   3. keywords
     *   4. numbers
     * Annotations are applied by re-scanning the source for each category and
     * using [addStyle] with explicit start/end offsets; later passes for the
     * same range win, so we add comments last to ensure they grey out the
     * keywords inside them.
     */
    private fun highlightGeneric(
        code: String,
        keywords: Set<String>,
        commentSingle: String? = null,
        commentMulti: Pair<String, String>? = null,
        tripleQuotes: Boolean = false
    ): AnnotatedString = buildAnnotatedString {
        val spans = mutableListOf<Span>()

        // Strings — "..." or '...'
        stringPattern.findAll(code).forEach { m ->
            spans.add(Span(m.range.first, m.range.last + 1, Palette.String))
        }
        // Triple-quoted Python strings — """..."""
        if (tripleQuotes) {
            tripleQuotedPattern.findAll(code).forEach { m ->
                spans.add(Span(m.range.first, m.range.last + 1, Palette.String))
            }
        }

        // Numbers
        numberPattern.findAll(code).forEach { m ->
            // Avoid recolouring the inside a string/identifier
            if (spans.none { it.contains(m.range.first) } &&
                (m.range.first == 0 || !code[m.range.first - 1].isLetterOrDigit())) {
                spans.add(Span(m.range.first, m.range.last + 1, Palette.Number))
            }
        }

        // Keywords (word-boundary)
        keywordPattern.findAll(code).forEach { m ->
            val word = m.value
            if (word in keywords) {
                spans.add(Span(m.range.first, m.range.last + 1, Palette.Keyword))
            }
        }

        // Comments — applied last so they grey out keywords inside them
        if (commentSingle != null) {
            lineCommentPattern(commentSingle).findAll(code).forEach { m ->
                spans.add(Span(m.range.first, m.range.last + 1, Palette.Comment))
            }
        }
        if (commentMulti != null) {
            blockCommentPattern(commentMulti.first, commentMulti.second).findAll(code).forEach { m ->
                spans.add(Span(m.range.first, m.range.last + 1, Palette.Comment))
            }
        }

        // Resolve overlaps — last-wins (already in insertion order: comments last)
        spans.sortBy { it.start }
        append(code)
        val resolved = resolveOverlaps(spans)
        for (s in resolved) {
            addStyle(SpanStyle(color = s.color, fontFamily = FontFamily.Monospace), s.start, s.end)
        }
    }

    private fun highlightSql(code: String): AnnotatedString = highlightGeneric(
        code,
        keywords = Keywords.SQL,
        commentSingle = "--",
        commentMulti = "/*" to "*/"
    )

    private fun highlightJson(code: String): AnnotatedString = buildAnnotatedString {
        // Keys: "field":  → key colour; values stay default
        append(code)
        jsonKeyPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Attr, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        // Strings
        stringPattern.findAll(code).forEach { m ->
            // Skip strings that are json keys (already coloured)
            if (jsonKeyPattern.findAll(code).none { it.range.first == m.range.first }) {
                addStyle(SpanStyle(color = Palette.String, fontFamily = FontFamily.Monospace),
                    m.range.first, m.range.last + 1)
            }
        }
        numberPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Number, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
    }

    private fun highlightMarkup(code: String): AnnotatedString = buildAnnotatedString {
        append(code)
        // Tags <tag> or </tag>
        markupTagPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Tag, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        // Attribute names
        markupAttrPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Attr, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        // Attribute values "..."
        stringPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.String, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        // Comments <!-- ... -->
        markupCommentPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Comment, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
    }

    private fun highlightCss(code: String): AnnotatedString = buildAnnotatedString {
        append(code)
        // Property names: `color:` / `font-size:`
        cssPropPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Attr, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        // Selectors before `{` — heuristic: a line ending with `{`
        // Values (strings / numbers)
        stringPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.String, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        numberPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Number, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        cssCommentPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Comment, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
    }

    private fun highlightYaml(code: String): AnnotatedString = buildAnnotatedString {
        append(code)
        // Keys at start of line (after indent): `name:` / `- item:`
        yamlKeyPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Attr, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        // Strings
        stringPattern.findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.String, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
        // Comments
        lineCommentPattern("#").findAll(code).forEach { m ->
            addStyle(SpanStyle(color = Palette.Comment, fontFamily = FontFamily.Monospace),
                m.range.first, m.range.last + 1)
        }
    }

    // ----- Overlap resolution -----------------------------------------------------

    private data class Span(val start: Int, val end: Int, val color: Color) {
        fun contains(idx: Int) = idx in start until end
    }

    /**
     * Sorts spans by start; keeps later-inserted spans on top when ranges
     * overlap. Returns a list with no overlaps.
     */
    private fun resolveOverlaps(spans: List<Span>): List<Span> {
        if (spans.isEmpty()) return emptyList()
        val sorted = spans.sortedWith(compareBy({ it.start }, { it.end }))
        val out = mutableListOf<Span>()
        for (s in sorted) {
            // Find overlaps with already accepted spans — last (most-recent) wins,
            // which by insertion order means comments (added last) win over
            // keywords/numbers/strings.
            val conflicts = out.filter { it.start < s.end && s.start < it.end }
            if (conflicts.isEmpty()) {
                out.add(s)
                continue
            }
            // Drop conflicting older spans and add this one.
            out.removeAll(conflicts.toSet())
            out.add(s)
        }
        return out
    }

    // ----- Patterns ---------------------------------------------------------------

    private val stringPattern = Regex("""\"(?:\\.|[^"\\])*\"|'(?:\\.|[^'\\])*'""")
    private val tripleQuotedPattern = Regex("\"\"\"[\\s\\S]*?\"\"\"")
    private val numberPattern = Regex("\\b\\d+(?:\\.\\d+)?(?:[eE][-+]?\\d+)?\\b")
    private val keywordPattern = Regex("\\b[A-Za-z_]\\w*\\b")

    private val jsonKeyPattern = Regex("\"[^\"]*\"(?=\\s*:)")
    private val markupTagPattern = Regex("</?[A-Za-z][\\w:-]*|/?>")
    private val markupAttrPattern = Regex("\\b[A-Za-z_:][-\\w:]* (?==)")
    private val markupCommentPattern = Regex("<!--[\\s\\S]*?-->")
    private val cssPropPattern = Regex("(?m)^\\s*[-A-Za-z_][-\\w]*\\s*(?=[:;])")
    private val cssCommentPattern = Regex("/\\*[\\s\\S]*?\\*/")
    private val yamlKeyPattern = Regex("(?m)^\\s*(-\\s+)?[A-Za-z_][-\\w]*:")

    private fun lineCommentPattern(prefix: String): Regex {
        val esc = Regex.escape(prefix)
        return Regex("(?m)^.*$esc.*$|(?m)(?<=\\s)$esc.*$")
    }

    private fun blockCommentPattern(open: String, close: String): Regex {
        val o = Regex.escape(open)
        val c = Regex.escape(close)
        return Regex("$o[\\s\\S]*?$c")
    }

    // ----- Keyword sets -----------------------------------------------------------

    private object Keywords {
        val PYTHON = setOf(
            "False", "None", "True", "and", "as", "assert", "async", "await", "break",
            "class", "continue", "def", "del", "elif", "else", "except", "finally",
            "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
            "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
            "print", "len", "range", "open", "self"
        )

        val JS = setOf(
            "abstract", "async", "await", "break", "case", "catch", "class", "const",
            "continue", "debugger", "default", "delete", "do", "else", "enum", "export",
            "extends", "false", "finally", "for", "from", "function", "if", "implements",
            "import", "in", "instanceof", "interface", "let", "new", "null", "of",
            "package", "private", "protected", "public", "return", "static", "super",
            "switch", "this", "throw", "true", "try", "type", "typeof", "undefined",
            "var", "void", "while", "with", "yield", "console"
        )

        val TS = JS + setOf(
            "any", "boolean", "number", "string", "unknown", "never", "object",
            "as", "is", "keyof", "namespace", "readonly", "declare"
        )

        val KOTLIN = setOf(
            "as", "break", "class", "continue", "do", "else", "false", "for", "fun",
            "if", "in", "interface", "is", "null", "object", "package", "return",
            "super", "this", "throw", "true", "try", "typealias", "typeof", "val",
            "var", "when", "while", "by", "catch", "finally", "get", "import", "in",
            "init", "param", "property", "receiver", "set", "value", "where",
            "private", "protected", "internal", "public", "abstract", "annotation",
            "companion", "const", "crossinline", "data", "enum", "final", "infix",
            "inline", "inner", "lateinit", "noinline", "open", "operator", "out",
            "override", "reified", "sealed", "suspend", "tailrec", "vararg",
            "field"
        )

        val JAVA = setOf(
            "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
            "class", "const", "continue", "default", "do", "double", "else", "enum",
            "extends", "false", "final", "finally", "float", "for", "goto", "if",
            "implements", "import", "instanceof", "int", "interface", "long", "native",
            "new", "null", "package", "private", "protected", "public", "return",
            "short", "static", "strictfp", "super", "switch", "synchronized", "this",
            "throw", "throws", "transient", "true", "try", "void", "volatile", "while",
            "var"
        )

        val GO = setOf(
            "break", "case", "chan", "const", "continue", "default", "defer", "else",
            "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
            "map", "package", "range", "return", "select", "struct", "switch", "type",
            "var", "true", "false", "nil", "iota", "make", "len", "cap", "append"
        )

        val RUST = setOf(
            "as", "break", "const", "continue", "crate", "else", "enum", "extern",
            "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
            "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct",
            "super", "trait", "true", "type", "unsafe", "use", "where", "while",
            "async", "await", "dyn"
        )

        val SQL = setOf(
            "SELECT", "FROM", "WHERE", "INSERT", "UPDATE", "DELETE", "INTO", "VALUES",
            "AND", "OR", "NOT", "NULL", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
            "ON", "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "OFFSET", "CREATE",
            "TABLE", "INDEX", "DROP", "ALTER", "ADD", "PRIMARY", "KEY", "FOREIGN",
            "REFERENCES", "UNIQUE", "DEFAULT", "AUTOINCREMENT", "AS", "DISTINCT",
            "CASE", "WHEN", "THEN", "ELSE", "END", "UNION", "ALL", "EXISTS", "BETWEEN",
            "LIKE", "IN", "IS"
        )

        val BASH = setOf(
            "if", "then", "else", "elif", "fi", "for", "do", "done", "while", "until",
            "case", "esac", "function", "in", "return", "break", "continue", "echo",
            "export", "local", "readonly", "unset", "set", "shift", "source", "alias",
            "cd", "ls", "cp", "mv", "rm", "mkdir", "rmdir", "cat", "grep", "sed",
            "awk", "find", "chmod", "chown", "sudo", "apt", "yum", "brew", "git",
            "true", "false"
        )
    }
}
