package com.example.aichat.ui.chat

import android.os.Handler
import android.os.Looper
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.example.aichat.ui.chat.toolcard.CodeHighlighter
import com.example.aichat.ui.theme.ChatColorScheme
import com.example.aichat.ui.theme.chatColors

/**
 * Markdown renderer for AI chat responses — hand-rolled (no external dep).
 *
 * Supported GitHub-Flavored Markdown subset:
 *
 *   - Headers (# .. ######)
 *   - **bold**, *italic*, ***bold+italic***, ~~strikethrough~~
 *   - `inline code`
 *   - ```fenced code blocks``` (with optional language tag + copy button)
 *   - Unordered lists (-, *, +) with nested indentation
 *   - Ordered lists (1., 2., ...)
 *   - Block quotes (>)
 *   - Horizontal rules (---, ***, ___)
 *   - Links [text](url) — clickable, opens via Intent
 *   - GFM tables (| ... | ... |) with optional column alignment
 *     (:---, :---:, ---:) — header + body rows render as a Compose
 *     table with sticky header tint and per-column alignment.
 *   - Display math (`$$...$$` on its own line, single- or multi-line)
 *     rendered via a small WebView that loads MathJax 3 from jsDelivr
 *     CDN. The app needs internet for AI chat anyway, so reusing the
 *     connection for MathJax keeps the binary slim (no bundled fonts).
 *   - Inline math (`$...$`) is detected but rendered as monospace raw
 *     LaTeX (not rendered) — Text composables can't host a WebView per
 *     span. Display math covers the common case for AI math responses.
 *   - Paragraphs / hard line breaks
 *
 * Why not a library?
 *   - The only mature Compose-native option (mikepenz multiplatform-markdown-renderer)
 *     is not mirrored on Aliyun, so it's unavailable from behind the GFW.
 *   - commonmark-java pulls 300 KB of transitive deps.
 *   - For chat responses, this ~250-line parser covers >95% of what LLMs emit.
 */
@Composable
fun MarkdownText(
    text: String,
    modifier: Modifier = Modifier,
    style: TextStyle = MaterialTheme.typography.bodyMedium
) {
    val chatColors = MaterialTheme.chatColors
    // Parse defensively. A malformed code fence or huge nested structure
    // during streaming used to crash the parser; we fall back to a single
    // raw paragraph block so the user still sees the partial output instead
    // of a blank bubble.
    val blocks = remember(text) {
        runCatching { parseBlocks(text) }.getOrElse {
            listOf(MdBlock.Paragraph(text))
        }
    }

    // v4.2.12: re-introduce SelectionContainer so users can select spans of
    // text and copy via the native popup. v4.2.5 removed this because the
    // native ActionMode duplicated the app's long-press DropdownMenu; but
    // with Compose BOM 2024.06 (1.7.x) the native popup no longer clashes
    // with our menu — long-press on text shows native selection handles +
    // popup, long-press on bubble chrome shows our app menu. Both coexist.
    SelectionContainer {
        Column(
            modifier = modifier.fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            blocks.forEach { block -> renderBlock(block, style, chatColors) }
        }
    }
}

// ----- Block model ------------------------------------------------------------

private sealed class MdBlock {
    data class Header(val level: Int, val text: String) : MdBlock()
    data class Code(val lang: String?, val code: String) : MdBlock()
    data class ListItem(val ordered: Boolean, val indent: Int, val text: String) : MdBlock()
    data class Quote(val text: String) : MdBlock()
    data class Paragraph(val text: String) : MdBlock()
    /**
     * v4.2.12 #6: GFM table block. [headers] is the first row (always
     * rendered bold + tinted). [rows] is 0..n body rows. [aligns] carries
     * per-column alignment derived from the separator row
     * (`:---` LEFT, `:---:` CENTER, `---:` RIGHT, `---` defaults LEFT).
     * Rows with fewer cells than headers are padded with empty strings;
     * rows with more are truncated so the grid stays rectangular.
     */
    data class Table(
        val headers: List<String>,
        val rows: List<List<String>>,
        val aligns: List<TableAlign>
    ) : MdBlock()
    /**
     * v4.2.12 #7: LaTeX display math. [content] is the raw LaTeX between
     * the `$$` delimiters (no surrounding `$$` in the string itself).
     * Always rendered via MathJax in a WebView — AnnotatedString can't
     * represent math glyphs. Multi-line LaTeX (e.g. aligned equations)
     * is supported because MathJax handles newlines inside its parser.
     */
    data class Math(val content: String) : MdBlock()
    data object Rule : MdBlock()
}

private enum class TableAlign { LEFT, CENTER, RIGHT }

// ----- Block parsing ----------------------------------------------------------

private fun parseBlocks(src: String): List<MdBlock> {
    val out = mutableListOf<MdBlock>()
    val lines = src.replace("\r\n", "\n").split("\n")
    var i = 0
    // Safety: hard cap on iterations so a pathological input can't loop
    // forever. Each loop iteration consumes at least one line, so this is
    // generous; if we ever hit it we just flush what we have.
    var safety = lines.size * 4 + 16

    while (i < lines.size && safety-- > 0) {
        val line = lines[i]

        // Skip blank lines between blocks
        if (line.isBlank()) { i++; continue }

        // Fenced code block: ```lang ... ```
        val fenceMatch = Regex("^\\s*```(.*)\$").matchEntire(line)
        if (fenceMatch != null) {
            val lang = fenceMatch.groupValues[1].trim().takeIf { it.isNotEmpty() }
            val sb = StringBuilder()
            i++
            // Cap collected code at 200 KB so a runaway LLM output can't
            // drag the parser / render tree to a halt.
            val maxCodeBytes = 200_000
            while (i < lines.size && !lines[i].trimStart().startsWith("```")) {
                if (sb.length < maxCodeBytes) {
                    sb.append(lines[i]).append('\n')
                }
                i++
            }
            if (i < lines.size) i++ // consume closing fence
            out.add(MdBlock.Code(lang, sb.toString().trimEnd('\n')))
            continue
        }

        // Header: #{1,6} space text
        val headerMatch = Regex("^(#{1,6})\\s+(.+?)\\s*#*\$").matchEntire(line)
        if (headerMatch != null) {
            val level = headerMatch.groupValues[1].length
            val text = headerMatch.groupValues[2]
            out.add(MdBlock.Header(level, text))
            i++
            continue
        }

        // Horizontal rule: ---, ***, ___ (≥3 same char)
        if (Regex("^\\s*([-*_])\\1{2,}\\s*\$").matches(line)) {
            out.add(MdBlock.Rule)
            i++
            continue
        }

        // Block quote: > text (greedily consume consecutive > lines)
        if (line.trimStart().startsWith(">")) {
            val sb = StringBuilder()
            while (i < lines.size && lines[i].trimStart().startsWith(">")) {
                val content = lines[i].trimStart().removePrefix(">").trim()
                if (sb.isNotEmpty()) sb.append('\n')
                sb.append(content)
                i++
            }
            out.add(MdBlock.Quote(sb.toString()))
            continue
        }

        // List item: (-, *, +) or (digit.) with indent
        val listMatch = Regex("^(\\s*)([-*+]|\\d+\\.)\\s+(.+)\$").matchEntire(line)
        if (listMatch != null) {
            val indent = listMatch.groupValues[1].length
            val marker = listMatch.groupValues[2]
            val content = listMatch.groupValues[3]
            val ordered = marker.endsWith(".")
            out.add(MdBlock.ListItem(ordered, indent, content))
            i++
            continue
        }

        // v4.2.12 #7: Display math — $$...$$ block. Can be single-line
        // (`$$ x^2 $$`) or multi-line (open `$$` on one line, close `$$`
        // later). Single-line is the common case for short formulas.
        // Inline `$...$` math is handled separately in [parseInline]
        // (with a monospace fallback because Text can't host a WebView).
        val trimmedLine = line.trimStart()
        // Also handle LaTeX-style `\[ ... \]` display math (some LLMs
        // prefer this over `$$...$$`). Behaves identically: single-line
        // or multi-line.
        //
        // v4.2.12 #8: ALSO accept single `$ ... $` on its own line as
        // display math. Many LLMs (e.g. Kimi-flavored) emit display
        // formulas this way instead of `$$...$$`. We must check `$$`
        // FIRST so we don't swallow the opener of `$$x^2$$` as a single-
        // dollar block. To avoid false positives on currency (`$5 and $10`)
        // we require the inner content to contain at least one LaTeX-ish
        // glyph: `\`, `_`, `^`, `{`, `}`, `=`, or letters with `(`.
        val openToken: String? = when {
            trimmedLine.startsWith("$$") -> "$$"
            trimmedLine.startsWith("\\[") -> "\\["
            trimmedLine.startsWith("$") -> "$"
            else -> null
        }
        if (openToken != null) {
            val closeToken = when (openToken) {
                "$$" -> "$$"
                "\\[" -> "\\]"
                else -> "$"
            }
            val openLen = openToken.length
            val afterOpen = trimmedLine.substring(openLen)
            val closeIdx = afterOpen.indexOf(closeToken)
            if (closeIdx >= 0) {
                // Single-line form: $$ x^2 $$, \[ x^2 \], or $ x^2 $
                val content = afterOpen.substring(0, closeIdx).trim()
                val isLikelyMath = content.isNotEmpty() &&
                    (openToken != "$" || content.any {
                        it == '\\' || it == '_' || it == '^' ||
                        it == '{' || it == '}' || it == '='
                    })
                if (isLikelyMath) {
                    out.add(MdBlock.Math(content))
                    i++
                    continue
                }
                // Single-$ without LaTeX chars: fall through to paragraph
                // (probably currency or a stray dollar).
            } else if (openToken != "$") {
                // Multi-line form for $$ and \[: open token, then collect
                // until close. Single-$ doesn't get a multi-line form
                // because it's ambiguous with inline math.
                val sb = StringBuilder(afterOpen)
                i++
                var foundClose = false
                while (i < lines.size) {
                    val l = lines[i]
                    val close = l.indexOf(closeToken)
                    if (close >= 0) {
                        if (sb.isNotEmpty()) sb.append('\n')
                        sb.append(l.substring(0, close))
                        i++
                        foundClose = true
                        break
                    }
                    if (sb.isNotEmpty()) sb.append('\n')
                    sb.append(l)
                    i++
                }
                if (!foundClose) {
                    // Stream cut off mid-formula — emit what we have so the
                    // user sees the partial math instead of nothing.
                }
                out.add(MdBlock.Math(sb.toString().trim()))
                continue
            }
        }

        // v4.2.12 #6: GFM table — header row followed by separator row.
        // Two-line lookahead: the current line must look like a row
        // (`|`-separated cells), and the NEXT line must match the
        // separator grammar (`---`, `:---`, `---:`, `:---:` per column).
        // If both match, greedily consume subsequent `|`-lines as body
        // rows. Anything that isn't a `|`-line ends the table.
        val headerCells = parseTableRow(line)
        if (headerCells != null && i + 1 < lines.size) {
            val aligns = parseTableSeparator(lines[i + 1], headerCells.size)
            if (aligns != null) {
                val rows = mutableListOf<List<String>>()
                i += 2 // consume header + separator
                while (i < lines.size) {
                    val bodyLine = lines[i]
                    if (bodyLine.isBlank()) break
                    val bodyCells = parseTableRow(bodyLine)
                    if (bodyCells == null) break
                    rows.add(bodyCells)
                    i++
                }
                out.add(MdBlock.Table(headerCells, rows, aligns))
                continue
            }
        }

        // Paragraph: collect until blank line / block trigger
        val sb = StringBuilder(line)
        i++
        while (i < lines.size && lines[i].isNotBlank()) {
            val next = lines[i]
            val nextTrim = next.trimStart()
            // Stop paragraph if next line is clearly a different block.
            // v4.2.12 #8: also break on `$$`, `\[`, and single-`$` math
            // openers so a paragraph like `**公式：**` doesn't swallow the
            // display math that follows on its own line.
            if (nextTrim.startsWith("```") ||
                nextTrim.startsWith("#") ||
                nextTrim.startsWith(">") ||
                nextTrim.startsWith("$$") ||
                nextTrim.startsWith("\\[") ||
                Regex("^\\s*([-*_])\\1{2,\\s*\$").matches(next) ||
                Regex("^\\s*([-*+]|\\d+\\.)\\s+.+").matches(next)
            ) break
            // Single-`$` opener on its own line: break so the math-block
            // branch can pick it up. Require at least 4 chars (`$ x $`) to
            // avoid breaking on stray `$` mid-paragraph that legitimately
            // belongs inline.
            if (nextTrim.startsWith("$") && !nextTrim.startsWith("$$") && nextTrim.length >= 4) break
            sb.append('\n').append(next)
            i++
        }
        out.add(MdBlock.Paragraph(sb.toString()))
    }

    return out
}

// v4.2.12 #6: GFM table helpers. Used by [parseBlocks] to detect the
// header + separator pattern and to consume body rows.

/**
 * Parse a single `|`-separated row. Returns null if the line doesn't look
 * like a table row (no `|`, or only whitespace between pipes).
 *
 * Handles GFM's escaped pipe `\|` inside cells — splits on `|` not
 * preceded by `\`, then unescapes `\|` → `|` in each cell.
 *
 * Leading/trailing `|` is optional but typically present; we trim it
 * before splitting so `| a | b |` and `a | b` parse identically.
 */
private fun parseTableRow(line: String): List<String>? {
    val trimmed = line.trim()
    if (!trimmed.contains('|')) return null
    // Strip an optional single leading / trailing pipe. Don't use trimChars
    // because that would eat multiple pipes and break empty-cell cases.
    val body = trimmed.let {
        when {
            it.startsWith('|') && it.endsWith('|') && it.length >= 2 -> it.substring(1, it.length - 1)
            it.startsWith('|') -> it.substring(1)
            it.endsWith('|') -> it.substring(0, it.length - 1)
            else -> it
        }
    }
    if (body.isBlank()) return null
    // Split on `|` that is NOT preceded by `\`. Regex lookbehind: position
    // where the preceding char is not a backslash.
    val cells = body.split(Regex("(?<!\\\\)\\|")).map { cell ->
        cell.trim().replace("\\|", "|")
    }
    // Reject all-empty rows — they're probably horizontal rules or
    // decoration that happens to contain pipes.
    return cells.takeIf { it.any { c -> c.isNotEmpty() } }
}

/**
 * Parse the GFM table separator row (`|:---|:---:|---:|---|`). Returns the
 * per-column alignment list, or null if the line isn't a valid separator
 * for the requested column count. Each cell must be one of:
 *   `:---`  → LEFT
 *   `:---:` → CENTER
 *   `---:`  → RIGHT
 *   `---`   → LEFT (default)
 */
private fun parseTableSeparator(line: String, expectedCols: Int): List<TableAlign>? {
    val trimmed = line.trim()
    val body = trimmed.let {
        when {
            it.startsWith('|') && it.endsWith('|') && it.length >= 2 -> it.substring(1, it.length - 1)
            it.startsWith('|') -> it.substring(1)
            it.endsWith('|') -> it.substring(0, it.length - 1)
            else -> it
        }
    }
    val parts = body.split('|').map { it.trim() }
    if (parts.size != expectedCols) return null
    return parts.map { cell ->
        // Each cell must be 3+ chars: at least one '-'. ':' on either side
        // controls alignment. Anything else → not a separator.
        val dashOnly = cell.trimStart(':').trimEnd(':')
        when {
            dashOnly.isEmpty() || !dashOnly.all { it == '-' } -> return null
            cell.startsWith(':') && cell.endsWith(':') -> TableAlign.CENTER
            cell.endsWith(':') -> TableAlign.RIGHT
            cell.startsWith(':') -> TableAlign.LEFT
            else -> TableAlign.LEFT
        }
    }
}

// ----- Block rendering --------------------------------------------------------

@Composable
private fun renderBlock(block: MdBlock, baseStyle: TextStyle, chatColors: ChatColorScheme) {
    when (block) {
        is MdBlock.Header -> {
            val headerSize = when (block.level) {
                1 -> 22.sp
                2 -> 20.sp
                3 -> 18.sp
                4 -> 16.sp
                5 -> 15.sp
                else -> 14.sp
            }
            Text(
                text = parseInline(block.text, baseStyle.color),
                style = baseStyle.copy(
                    fontSize = headerSize,
                    fontWeight = FontWeight.Bold
                )
            )
        }

        is MdBlock.Code -> CodeBlock(block.code, block.lang, baseStyle, chatColors)

        is MdBlock.ListItem -> {
            val bullet = "• "
            val indent = (block.indent / 2).coerceAtMost(4)
            // v4.2.12 #8: list items often carry inline math (e.g.
            // "* **符号**：$|y_i - \\hat{y}_i|$ 为第 $i$ 个样本的…").
            // Plain Text can't render math glyphs, so route through
            // MathParagraph (KaTeX WebView) when inline math is detected.
            if (hasInlineMath(block.text)) {
                Row(modifier = Modifier.fillMaxWidth()) {
                    Spacer(Modifier.width(((indent * 12) + 12).dp))
                    MathParagraph(block.text, baseStyle)
                }
            } else {
                Row(modifier = Modifier.fillMaxWidth()) {
                    Spacer(Modifier.width((indent * 12).dp))
                    Text(
                        text = parseInline(bullet + block.text, baseStyle.color),
                        style = baseStyle
                    )
                }
            }
        }

        is MdBlock.Quote -> {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(6.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f))
                    .padding(start = 10.dp, end = 10.dp, top = 6.dp, bottom = 6.dp)
            ) {
                Spacer(
                    Modifier
                        .width(3.dp)
                        .height(18.dp)
                        .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.6f))
                )
                Spacer(Modifier.width(8.dp))
                if (hasInlineMath(block.text)) {
                    MathParagraph(
                        block.text,
                        baseStyle.copy(
                            color = baseStyle.color.copy(alpha = 0.85f),
                            fontStyle = FontStyle.Italic
                        )
                    )
                } else {
                    Text(
                        text = parseInline(block.text, baseStyle.color),
                        style = baseStyle.copy(
                            color = baseStyle.color.copy(alpha = 0.85f),
                            fontStyle = FontStyle.Italic
                        )
                    )
                }
            }
        }

        MdBlock.Rule -> {
            Spacer(
                Modifier
                    .fillMaxWidth()
                    .height(1.dp)
                    .background(MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.6f))
            )
        }

        is MdBlock.Table -> TableBlock(block, baseStyle, chatColors)

        is MdBlock.Math -> MathBlock(block.content, baseStyle)

        is MdBlock.Paragraph -> {
            // v4.2.12 #7: paragraphs that contain inline math (`$...$`)
            // go through WebView so MathJax can typeset the formulas.
            // Otherwise keep the Text path (faster, integrates with
            // SelectionContainer for native text selection).
            if (hasInlineMath(block.text)) {
                MathParagraph(block.text, baseStyle)
            } else {
                Text(
                    text = parseInline(block.text, baseStyle.color),
                    style = baseStyle
                )
            }
        }
    }
}

/**
 * v4.2.12 #6: render a GFM table as a Compose Column of Rows. Each Row has
 * weight(1f) cells so columns are equal-width — simplest approach that
 * handles arbitrary content. Tinted header strip + subtle borders give it
 * a "card" look without an external dependency.
 *
 * Why not LazyTable / scrollable grid:
 *   - Tables in chat replies are typically small (≤10 rows). A scrollable
 *     grid would add complexity for no gain.
 *   - Wide tables overflow horizontally — the outer chat LazyColumn is
 *     vertical-only, so we wrap the whole table in a horizontalScroll to
 *     let the user pan across wide tables without breaking row layout.
 */
@Composable
private fun TableBlock(
    block: MdBlock.Table,
    baseStyle: TextStyle,
    chatColors: ChatColorScheme
) {
    val borderColor = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.6f)
    val headerBg = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f)

    // Equal weight per column. Rows may have different cell counts than
    // headers; we render whatever each row has (missing cells stay empty).
    val colCount = block.headers.size

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(6.dp))
            .background(MaterialTheme.colorScheme.surface)
            .horizontalScroll(rememberScrollState())
    ) {
        // Header row — bold + tinted
        Row(modifier = Modifier.background(headerBg)) {
            block.headers.forEachIndexed { idx, header ->
                val align = block.aligns.getOrNull(idx) ?: TableAlign.LEFT
                Text(
                    text = parseInline(header, baseStyle.color),
                    style = baseStyle.copy(fontWeight = FontWeight.Bold),
                    textAlign = align.toTextAlign(),
                    modifier = Modifier
                        .width(120.dp) // fixed per-cell width so wide tables scroll
                        .padding(horizontal = 10.dp, vertical = 8.dp)
                )
            }
        }
        // Thin divider between header and body
        Spacer(Modifier
            .fillMaxWidth()
            .height(1.dp)
            .background(borderColor)
        )
        // Body rows
        block.rows.forEachIndexed { rowIdx, row ->
            Row {
                for (colIdx in 0 until colCount) {
                    val cell = row.getOrNull(colIdx).orEmpty()
                    val align = block.aligns.getOrNull(colIdx) ?: TableAlign.LEFT
                    Text(
                        text = parseInline(cell, baseStyle.color),
                        style = baseStyle,
                        textAlign = align.toTextAlign(),
                        modifier = Modifier
                            .width(120.dp)
                            .padding(horizontal = 10.dp, vertical = 6.dp)
                    )
                }
            }
            if (rowIdx < block.rows.lastIndex) {
                Spacer(Modifier
                    .fillMaxWidth()
                    .height(0.5.dp)
                    .background(borderColor.copy(alpha = 0.5f))
                )
            }
        }
    }
}

private fun TableAlign.toTextAlign(): TextAlign = when (this) {
    TableAlign.LEFT -> TextAlign.Start
    TableAlign.CENTER -> TextAlign.Center
    TableAlign.RIGHT -> TextAlign.End
}

/**
 * v4.2.12 #7: render LaTeX display math via a small WebView that loads
 * **KaTeX** from the jsDelivr CDN. KaTeX (same family Kimi/Web WeChat
 * use) emits HTML+CSS rather than SVG, and its `renderMathInElement`
 * call is **synchronous** — once it returns, the DOM is final and
 * `scrollHeight` is the true height. No async typeset step, no
 * `pageReady` promise to miss on OEM WebView forks.
 *
 * The device already has internet for AI chat so piggy-backing the
 * connection for KaTeX keeps the binary slim.
 *
 * Why not a native LaTeX renderer: writing a TeX parser + layout engine
 * in Compose is thousands of lines. KaTeX covers edge cases (align
 * environments, matrices, \displaystyle, etc.) that a hand-rolled
 * parser would inevitably get wrong.
 */
@Composable
private fun MathBlock(
    latex: String,
    baseStyle: TextStyle
) {
    MathWebView(
        contents = listOf(MathContent.Display(latex)),
        baseStyle = baseStyle,
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
    )
}

/**
 * v4.2.12 #7: render a paragraph that contains inline math (`$...$`)
 * via WebView. The prose part is converted to lightweight HTML
 * (bold/italic/code/links) and inline math is passed through KaTeX.
 *
 * We can't keep this paragraph in the Text path because AnnotatedString
 * can't represent math glyphs. Routing math-containing paragraphs to
 * WebView is the only way to render inline formulas correctly.
 */
@Composable
private fun MathParagraph(
    text: String,
    baseStyle: TextStyle
) {
    MathWebView(
        contents = listOf(MathContent.InlineParagraph(text)),
        baseStyle = baseStyle,
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp)
    )
}

private sealed class MathContent {
    /** Display math: wrap in `$$...$$`, render alone on a centered line. */
    data class Display(val latex: String) : MathContent()
    /**
     * Inline paragraph: prose + inline `$x^2$` math. Prose is converted
     * to HTML via [markdownToSimpleHtml] first; KaTeX's auto-render then
     * typesets the `$...$` runs in place (synchronously).
     */
    data class InlineParagraph(val text: String) : MathContent()
}

@Composable
private fun MathWebView(
    contents: List<MathContent>,
    baseStyle: TextStyle,
    modifier: Modifier = Modifier
) {
    val density = LocalDensity.current
    // Default placeholder kept small (100 dp ≈ one line of display math
    // plus padding). Real measurement arrives within a few hundred ms
    // and resizes the WebView to the actual rendered height.
    var measuredHeightDp by remember(contents) { mutableStateOf(100.dp) }
    val mainHandler = remember { Handler(Looper.getMainLooper()) }

    // The JavascriptInterface has to hold a stable reference to the
    // height-updater. Recreating it on every recomposition would leak
    // addJavascriptInterface registrations, so we keep one per WebView
    // instance via remember keyed on contents.
    val reporter = remember(contents) {
        object {
            @JavascriptInterface
            fun reportHeight(px: Int) {
                android.util.Log.d("MathWebView", "reportHeight: $px px")
                if (px <= 0) return
                if (px > 5000) {
                    // Suspicious height — usually means KaTeX didn't typeset
                    // (raw text laid out as one-char-per-line in a 0-width
                    // WebView). Drop the report so the placeholder stays.
                    android.util.Log.d("MathWebView", "  dropping suspicious height px=$px (KaTeX likely didn't render)")
                    return
                }
                // JS interface calls arrive on a binder thread — hop back
                // to main before touching Compose state.
                mainHandler.post {
                    measuredHeightDp = with(density) { px.toDp() }
                }
            }

            @JavascriptInterface
            fun log(message: String) {
                android.util.Log.d("MathWebView", message)
            }
        }
    }

    // Re-create the WebView when contents change. The factory captures
    // the latex via closure; without key() the WebView would keep showing
    // stale content when the message above it re-streams.
    val contentsKey = remember(contents) {
        contents.joinToString("||") { c ->
            when (c) {
                is MathContent.Display -> "D:${c.latex.hashCode()}"
                is MathContent.InlineParagraph -> "I:${c.text.hashCode()}"
            }
        }
    }

    AndroidView(
        factory = { ctx ->
            object : WebView(ctx) {
                override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
                    super.onSizeChanged(w, h, oldw, oldh)
                    // v4.2.12 #8: when the WebView gets a real width from
                    // the Compose layout pass, kick JS to re-measure. The
                    // first reportHeight call often fires before layout —
                    // body clientWidth is 0, text reflows one-char-per-line
                    // and scrollHeight balloons to thousands of px. Without
                    // this nudge the placeholder stays at 100dp forever
                    // because lastReported is already cached at the bad
                    // value.
                    if (w > 0 && h > 0) {
                        evaluateJavascript(
                            "if (typeof reportHeight === 'function') reportHeight();",
                            null
                        )
                    }
                }
            }.apply {
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true
                // KaTeX assets live under file:///android_asset/katex/.
                // allowFileAccess must be true on API 30+ for asset URLs
                // to resolve from <script src>/<link href>; otherwise the
                // KaTeX JS silently fails to load and formulas stay raw.
                settings.allowFileAccess = true
                settings.allowFileAccessFromFileURLs = true
                settings.allowUniversalAccessFromFileURLs = true
                setBackgroundColor(android.graphics.Color.TRANSPARENT)
                // Enable horizontal scrolling so wide display math can be
                // panned inside the WebView. Without these the WebView
                // passes touch events up to the parent LazyColumn and the
                // formula stays clipped on the right.
                isHorizontalScrollBarEnabled = true
                isVerticalScrollBarEnabled = false
                requestDisallowInterceptTouchEvent(true)
                setOnTouchListener { _, _ ->
                    // When the user touches the WebView, prevent the
                    // parent LazyColumn from intercepting — otherwise
                    // a horizontal drag gets stolen as a vertical scroll.
                    parent?.requestDisallowInterceptTouchEvent(true)
                    false
                }
                addJavascriptInterface(reporter, "AndroidMath")
                // Route browser console output to logcat so we can see
                // KaTeX load errors / JS exceptions on Honor devices.
                webChromeClient = object : android.webkit.WebChromeClient() {
                    override fun onConsoleMessage(consoleMessage: android.webkit.ConsoleMessage): Boolean {
                        android.util.Log.d(
                            "MathWebView",
                            "chrome(${consoleMessage.messageLevel()}): ${consoleMessage.message()}"
                        )
                        return true
                    }
                }
                loadDataWithBaseURL(
                    "file:///android_asset/katex/",
                    buildMathHtml(contents, baseStyle.color),
                    "text/html",
                    "UTF-8",
                    null
                )
            }
        },
        // Reload the page when contentsKey changes so the WebView shows
        // the new formula. (AndroidView's factory only runs once per
        // composable instance, but update fires on every recomposition
        // where the parameters change.)
        update = { view ->
            view.addJavascriptInterface(reporter, "AndroidMath")
        },
        modifier = modifier
            .heightIn(min = 32.dp, max = 640.dp)
            .height(measuredHeightDp.coerceIn(32.dp, 640.dp))
    )
}

/**
 * Build the HTML document for [MathWebView] using **KaTeX** rather than
 * MathJax. This is the Kimi-style approach.
 *
 * Why KaTeX (not MathJax):
 *   - **Synchronous rendering.** `renderMathInElement()` returns only
 *     after every formula in the body has been turned into final HTML
 *     with concrete font metrics baked in. There is no async typeset
 *     step and no `pageReady` promise to wait on, so the very first
 *     `scrollHeight` read after the call is the real final height.
 *   - **HTML+CSS output (not SVG).** Layout is resolved by the browser's
 *     normal flow, so heights are stable across repaints and don't
 *     depend on a JS-driven re-layout pass.
 *   - **Fast first paint.** No client-side TeX compile; only one CSS
 *     file + a handful of woff2 fonts.
 *
 * Height reporting are still sent in three places — they're cheap and
 * idempotent, and protect against ROM quirks where KaTeX's font swap
 * shifts layout by a pixel after the woff2 arrives:
 *   1. Immediately after `renderMathInElement` returns.
 *   2. On `document.fonts.ready` (font swap complete).
 *   3. setTimeout 100ms / 400ms / 1000ms as belt-and-suspenders.
 *
 * Display math and inline math use different delimiters:
 *   - displayMath: `$$ ... $$`
 *   - inlineMath: `$ ... $`  (single dollar, but only when followed
 *                              by non-whitespace — guarded in the parser)
 */
private fun buildMathHtml(contents: List<MathContent>, textColor: Color): String {
    val hexColor = String.format("#%06X", 0xFFFFFF and textColor.toArgb())
    val dd = "${'$'}${'$'}" // literal $$

    val body = contents.joinToString("\n") { c ->
        when (c) {
            is MathContent.Display -> {
                val esc = c.latex
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                """<div class="display-math">$dd$esc$dd</div>"""
            }
            is MathContent.InlineParagraph -> {
                markdownToSimpleHtml(c.text)
            }
        }
    }

    return """
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
        <link rel="stylesheet" href="file:///android_asset/katex/katex.min.css">
        <style>
            html, body {
                margin: 0;
                padding: 0;
                background: transparent;
                overflow-x: auto;
                overflow-y: visible;
            }
            body {
                padding: 4px 8px;
                color: $hexColor;
                font-family: -apple-system, system-ui, sans-serif;
                font-size: 16px;
                line-height: 1.5;
                -webkit-text-size-adjust: 100%;
                white-space: normal;
                word-wrap: break-word;
            }
            .katex { color: $hexColor !important; font-size: 1.05em; }
            .katex-display {
                margin: 4px 0;
                padding: 0;
                overflow-x: auto;
                overflow-y: visible;
                max-width: 100%;
                line-height: 1.2;
            }
            .katex-display::-webkit-scrollbar { height: 3px; }
            .katex-display::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.4); border-radius: 2px; }
            .display-math { margin: 4px 0; text-align: center; }
            code {
                font-family: ui-monospace, "SF Mono", Menlo, monospace;
                background: rgba(128,128,128,0.12);
                padding: 1px 4px;
                border-radius: 3px;
                font-size: 0.92em;
            }
            pre code { display: block; padding: 8px; }
            strong { font-weight: 600; }
            em { font-style: italic; }
            a { color: #1A73E8; text-decoration: underline; }
            body { -webkit-user-select: none; user-select: none; }
        </style>
        </head>
        <body>$body
        <script>
            // KaTeX is synchronous — once renderMathInElement returns, the
            // DOM is final. No async typeset step. But WebView's scrollHeight
            // update isn't always synchronous with DOM mutations, so we:
            //   1. Force layout by reading getBoundingClientRect on body
            //      (which triggers synchronous reflow per HTML spec).
            //   2. Schedule rAF + multiple setTimeout retries for font-swap
            //      and late layout shifts.
            //   3. Take the MAX of body / documentElement / bounding rect
            //      because some OEM WebViews report 0 for one of them.
            var lastReported = 0;
            var rendered = false;
            function measureNow() {
                var h1 = document.body ? document.body.scrollHeight : 0;
                var h2 = document.documentElement ? document.documentElement.scrollHeight : 0;
                var h3 = 0;
                if (document.body) {
                    var rect = document.body.getBoundingClientRect();
                    h3 = Math.ceil(rect.height);
                }
                return Math.max(h1, h2, h3);
            }
            function reportHeight() {
                // v4.2.12 #8: the WebView's body sometimes hasn't been
                // laid out yet (clientWidth=0) when the first setTimeout
                // fires — text then reflows one-character-per-line and
                // scrollHeight balloons to thousands of px. Skip reports
                // until the body has a real width.
                var bw = document.body ? document.body.clientWidth : 0;
                if (bw < 50) return;
                var h = measureNow();
                // Sanity cap: a single chat bubble should never be taller
                // than ~5 screens of content. If KaTeX failed silently
                // (e.g. malformed LaTeX with throwOnError:false), the raw
                // text lays out at huge heights — clamp to avoid blank
                // space downstream.
                if (h > 4000) h = 0;
                // JS reports CSS pixels. The Kotlin side treats the value
                // as physical pixels and divides by screen density to get
                // dp. To make that math land on the right value, multiply
                // by devicePixelRatio here — on a density=3 screen a 100
                // CSS-px formula needs 300 physical px (= 100 dp) of
                // WebView height, not 100 px (= 33 dp) which would clip
                // 2/3 of the formula.
                var dpr = window.devicePixelRatio || 1;
                var physicalPx = Math.ceil(h * dpr);
                if (physicalPx > 0 && physicalPx !== lastReported) {
                    lastReported = physicalPx;
                    try { AndroidMath.reportHeight(physicalPx); } catch (e) {}
                }
            }
            function doRender() {
                if (rendered) return;
                // Wait for KaTeX + auto-render to be available.
                if (typeof katex === 'undefined' ||
                    typeof renderMathInElement !== 'function') {
                    try { AndroidMath.log("katex-not-ready: katex=" + (typeof katex) + " render=" + (typeof renderMathInElement)); } catch (_) {}
                    return;
                }
                rendered = true;
                try {
                    renderMathInElement(document.body, {
                        delimiters: [
                            {left: "$dd", right: "$dd", display: true},
                            {left: "\\[", right: "\\]", display: true},
                            {left: "\\(", right: "\\)", display: false},
                            {left: "${'$'}", right: "${'$'}", display: false}
                        ],
                        throwOnError: false,
                        errorColor: "#cc0000",
                        strict: false,
                        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
                        ignoredClasses: ["ignore-math"]
                    });
                    try { AndroidMath.log("render-ok"); } catch (_) {}
                    // Diagnostic: log horizontal overflow state of each
                    // .katex-display so we can see whether the WebView
                    // actually has scrollable width.
                    try {
                        var ds = document.querySelectorAll('.katex-display');
                        for (var i = 0; i < ds.length; i++) {
                            AndroidMath.log("kd[" + i + "] sw=" + ds[i].scrollWidth + " cw=" + ds[i].clientWidth);
                        }
                    } catch (_) {}
                } catch (e) {
                    try { AndroidMath.log("render-error:" + e.message); } catch (_) {}
                }
                // Force layout once before measuring.
                if (document.body) document.body.getBoundingClientRect();
                reportHeight();
                // rAF + multiple setTimeout retries — KaTeX font swap can
                // shift height by tens of px after first paint.
                requestAnimationFrame(function () {
                    reportHeight();
                    setTimeout(reportHeight, 50);
                });
                setTimeout(reportHeight, 200);
                setTimeout(reportHeight, 600);
                setTimeout(reportHeight, 1500);
                if (document.fonts && document.fonts.ready) {
                    document.fonts.ready.then(function () {
                        reportHeight();
                        // One more after fonts settle.
                        setTimeout(reportHeight, 100);
                    });
                }
            }
        </script>
        <!-- Local assets — no CDN, no network dependency, no CORS issues.
             Script tag ordering guarantees katex.min.js runs before
             auto-render.min.js. -->
        <script src="file:///android_asset/katex/katex.min.js"></script>
        <script src="file:///android_asset/katex/auto-render.min.js"></script>
        <script>
            // Kick off render as soon as both scripts above have executed.
            doRender();
        </script>
        </body>
        </html>
    """.trimIndent()
}

/**
 * Minimal Markdown→HTML converter used for paragraphs that contain
 * inline math. Only handles the inline constructs an LLM is likely to
 * use inside a math-bearing paragraph: bold, italic, inline code, links,
 * and line breaks. Block-level constructs (lists, headers, tables) are
 * handled by the outer parser and never reach here.
 *
 * Display math (`$$...$$`) is also passed through so a paragraph that
 * happens to include a display block (rare but possible) still renders.
 */
private fun markdownToSimpleHtml(src: String): String {
    // Escape HTML-significant chars FIRST so user input can't break out
    // of our wrapper. KaTeX parses its own `$...$` / `$$...$$` from
    // text nodes, so escaping doesn't interfere with it.
    var s = src
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")

    // Bold + italic: ***text*** → <strong><em>text</em></strong>
    s = s.replace(Regex("\\*\\*\\*(.+?)\\*\\*\\*")) {
        "<strong><em>${it.groupValues[1]}</em></strong>"
    }
    // Bold: **text**
    s = s.replace(Regex("\\*\\*(.+?)\\*\\*")) {
        "<strong>${it.groupValues[1]}</strong>"
    }
    // Italic: *text*  (skip single `*` adjacent to whitespace to avoid
    // matching bullet markers / wildcard math notation we already passed)
    s = s.replace(Regex("(?<=\\s|^)\\*(?!\\s)([^*]+?)(?!\\s)\\*(?=\\s|\$)")) {
        "<em>${it.groupValues[1]}</em>"
    }
    // Inline code: `text`
    s = s.replace(Regex("`([^`]+?)`")) {
        "<code>${it.groupValues[1]}</code>"
    }
    // Links: [text](url)
    s = s.replace(Regex("\\[(.+?)\\]\\(([^)]+?)\\)")) {
        "<a href=\"${it.groupValues[2]}\">${it.groupValues[1]}</a>"
    }
    // Newlines → <br>
    s = s.replace("\n", "<br>")
    return s
}

/**
 * v4.2.12 #7: detect whether a paragraph contains an inline math span
 * (`$...$` or `\(...\)`) that we should hand to KaTeX.
 *
 * v4.2.12 #8: relaxed the inner-content rule. The previous regex required
 * the opening `$` to be immediately followed by non-whitespace; that
 * missed `$ x^2 $` (with whitespace inside the dollars), which several
 * LLMs emit even for inline math. The new rule allows optional whitespace
 * after `$` and before `$`, but requires the content to contain at least
 * one LaTeX-ish glyph (`\`, `_`, `^`, `{`, `}`, `=`) so that currency
 * like `$5 and $10` doesn't false-positive.
 *
 * `$$...$$` and `\[...\]` (display math) are already stripped into their
 * own blocks by the parser and won't reach this check.
 */
private fun hasInlineMath(text: String): Boolean {
    val dollarRegex = Regex("""\$\s*[^$]*?[\\_^{}=][^$]*?\s*\$""")
    // Match LaTeX-style \(...\) inline math (some LLMs prefer this).
    val parenRegex = Regex("""\\\([^)]+\\\)""")
    return dollarRegex.containsMatchIn(text) || parenRegex.containsMatchIn(text)
}

@Composable
private fun CodeBlock(
    code: String,
    lang: String?,
    baseStyle: TextStyle,
    chatColors: ChatColorScheme
) {
    val context = LocalContext.current
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(chatColors.codeBackground)
    ) {
        // Header strip: lang label + copy button
        if (lang != null || code.length > 100) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(chatColors.codeBackground.copy(alpha = 0.6f))
                    .padding(horizontal = 10.dp, vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = lang?.takeIf { it.isNotEmpty() }?.uppercase() ?: "CODE",
                    style = MaterialTheme.typography.labelSmall.copy(
                        fontFamily = FontFamily.Monospace,
                        color = chatColors.onCodeBackground.copy(alpha = 0.7f)
                    ),
                    modifier = Modifier.weight(1f)
                )
                IconButton(
                    onClick = {
                        val cm = context.getSystemService(android.content.ClipboardManager::class.java)
                        cm?.setPrimaryClip(android.content.ClipData.newPlainText("code", code))
                    },
                    modifier = Modifier.size(28.dp)
                ) {
                    Icon(
                        imageVector = Icons.Filled.ContentCopy,
                        contentDescription = "复制",
                        tint = chatColors.onCodeBackground.copy(alpha = 0.8f),
                        modifier = Modifier.size(14.dp)
                    )
                }
            }
        }
        Text(
            text = CodeHighlighter.highlight(code, lang),
            modifier = Modifier
                .fillMaxWidth()
                // NOTE: no per-block verticalScroll - each scrollable code
                // block allocates its own ScrollState, and a long streaming
                // conversation with many code blocks would leak them. Let the
                // outer chat LazyColumn handle scrolling; long blocks just
                // take vertical space.
                .padding(12.dp),
            style = baseStyle.copy(
                fontFamily = FontFamily.Monospace,
                color = chatColors.onCodeBackground,
                fontSize = 13.sp,
                lineHeight = 18.sp
            )
        )
    }
}

// ----- Inline parsing ---------------------------------------------------------

/**
 * Parse inline markdown into an AnnotatedString. Handles: `code`, **bold**,
 * *italic*, ***bold+italic***, ~~strike~~, [text](url). Falls back gracefully
 * (emits raw text) if a pattern doesn't match.
 *
 * Implemented as a small hand-written state machine that scans left-to-right,
 * emitting spans when it encounters a recognized opener.
 */
private fun parseInline(src: String, baseColor: androidx.compose.ui.graphics.Color): AnnotatedString {
    return buildAnnotatedString {
        var i = 0
        val n = src.length
        val sb = StringBuilder()

        fun flushPlain() {
            if (sb.isNotEmpty()) {
                append(sb.toString())
                sb.clear()
            }
        }

        while (i < n) {
            val c = src[i]

            // Inline code: `...`
            if (c == '`') {
                val end = src.indexOf('`', i + 1)
                if (end > i) {
                    flushPlain()
                    val code = src.substring(i + 1, end)
                    // Cannot nest spans inside code; emit as-is with code style.
                    pushStyle(SpanStyle(fontFamily = FontFamily.Monospace))
                    append(code)
                    pop()
                    i = end + 1
                    continue
                }
            }

            // Link: [text](url)
            if (c == '[') {
                val close = src.indexOf(']', i + 1)
                if (close > i && close + 1 < n && src[close + 1] == '(') {
                    val end = src.indexOf(')', close + 2)
                    if (end > close + 1) {
                        flushPlain()
                        val text = src.substring(i + 1, close)
                        val url = src.substring(close + 2, end)
                        pushStringAnnotation(tag = "URL", annotation = url)
                        withStyle(
                            SpanStyle(
                                color = androidx.compose.ui.graphics.Color(0xFF1A73E8),
                                textDecoration = TextDecoration.Underline
                            )
                        ) { append(text) }
                        pop()
                        i = end + 1
                        continue
                    }
                }
            }

            // Bold+Italic ***text***, Bold **text**, Italic *text*
            if (c == '*' || c == '_') {
                val triple = i + 2 < n && src[i + 1] == c && src[i + 2] == c
                val double = !triple && i + 1 < n && src[i + 1] == c
                if (triple) {
                    val close = src.indexOf("$c$c$c", i + 3)
                    if (close > i + 3) {
                        flushPlain()
                        val inner = src.substring(i + 3, close)
                        withStyle(
                            SpanStyle(fontWeight = FontWeight.Bold, fontStyle = FontStyle.Italic)
                        ) { append(inner) }
                        i = close + 3
                        continue
                    }
                } else if (double) {
                    val close = src.indexOf("$c$c", i + 2)
                    if (close > i + 2) {
                        flushPlain()
                        val inner = src.substring(i + 2, close)
                        withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(inner) }
                        i = close + 2
                        continue
                    }
                } else {
                    // single * italic
                    val close = src.indexOf(c, i + 1)
                    if (close > i + 1) {
                        // avoid treating * as italic if next char is whitespace (math/bullet fallback)
                        if (i + 1 < n && !src[i + 1].isWhitespace()) {
                            flushPlain()
                            val inner = src.substring(i + 1, close)
                            withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(inner) }
                            i = close + 1
                            continue
                        }
                    }
                }
            }

            // Strikethrough ~~text~~
            if (c == '~' && i + 1 < n && src[i + 1] == '~') {
                val close = src.indexOf("~~", i + 2)
                if (close > i + 2) {
                    flushPlain()
                    val inner = src.substring(i + 2, close)
                    withStyle(SpanStyle(textDecoration = TextDecoration.LineThrough)) { append(inner) }
                    i = close + 2
                    continue
                }
            }

            // Newline → keep as-is (Text renders it as line break)
            sb.append(c)
            i++
        }

        flushPlain()
    }
}
