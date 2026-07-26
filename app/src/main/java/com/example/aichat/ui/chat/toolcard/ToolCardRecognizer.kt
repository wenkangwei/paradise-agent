package com.example.aichat.ui.chat.toolcard

/**
 * Splits a single AI message body into a list of [CardSegment]s so the renderer
 * can intersperse plain Markdown text with full-width "tool cards".
 *
 * Tool cards are recognised when the content contains EITHER:
 *   1. A complete HTML page (```<!DOCTYPE … </html>``` or a large block with
 *      both ```<html>``` and ```</html>```), OR
 *   2. A long fenced ```markdown``` block (more than [MARKDOWN_DOC_MIN_LINES]
 *      lines) — i.e. the model emitted a whole Markdown document as a tool.
 *
 * Everything else — including short fenced code blocks (```python, ```bash,
 * ```sql, etc.) — is returned as [CardSegment.Text]. The renderer keeps those
 * embedded in the Markdown flow with Kimi-style syntax highlighting rather
 * than breaking them out into cards.
 *
 * WHY split instead of letting MarkdownText render everything inline:
 *   HTML pages inside a chat bubble render at the bubble's max width (~80%
 *   of the screen), which is far too narrow for tables, complex layouts, or
 *   SVG visualisations. Splitting them out lets the card break out of the
 *   bubble and span the full content width, with its own full-screen /
 *   collect / share affordances.
 */
object ToolCardRecognizer {

    /** Lines of ```markdown fenced content past which it counts as a "document". */
    const val MARKDOWN_DOC_MIN_LINES = 20

    /**
     * Splits [content] into ordered [CardSegment]s. Preserves the original
     * text — segments concatenated in order reproduce the source verbatim
     * (modulo trailing-newline trimming).
     *
     * Defensive: never throws. If parsing somehow fails, the whole content
     * is returned as a single [CardSegment.Text] so the bubble still renders.
     */
    fun split(content: String): List<CardSegment> = runCatching {
        splitInternal(content)
    }.getOrElse { listOf(CardSegment.Text(content)) }

    private fun splitInternal(content: String): List<CardSegment> {
        if (content.isBlank()) return listOf(CardSegment.Text(content))

        val segments = mutableListOf<CardSegment>()
        val lines = content.split("\n")
        val textBuffer = StringBuilder()

        fun flushText() {
            if (textBuffer.isNotBlank()) {
                segments.add(CardSegment.Text(textBuffer.toString().trimEnd('\n')))
                textBuffer.setLength(0)
            }
        }

        var i = 0
        while (i < lines.size) {
            val line = lines[i]
            val fence = fenceLang(line)

            if (fence != null) {
                // Collect lines until the matching closing fence (``` or ~~~).
                val closeIdx = (i + 1 until lines.size).firstOrNull { j ->
                    val t = lines[j].trimStart()
                    t.startsWith("```") || t.startsWith("~~~")
                } ?: lines.size
                val body = lines.subList(i + 1, closeIdx).joinToString("\n")
                val closingFenceLine = if (closeIdx < lines.size) lines[closeIdx] else "```"

                when {
                    fence.equals("html", ignoreCase = true) && looksLikeHtmlPage(body) -> {
                        flushText()
                        segments.add(CardSegment.Tool(body, ToolType.HTML))
                    }
                    (fence.equals("markdown", ignoreCase = true) ||
                        fence.equals("md", ignoreCase = true)) &&
                        body.lineSequence().filter { it.isNotBlank() }.count() >= MARKDOWN_DOC_MIN_LINES -> {
                        flushText()
                        segments.add(CardSegment.Tool(body, ToolType.MARKDOWN))
                    }
                    // Fenced block with no language tag but the body itself is
                    // a full HTML page — model sometimes does this.
                    fence.isEmpty() && looksLikeHtmlPage(body) -> {
                        flushText()
                        segments.add(CardSegment.Tool(body, ToolType.HTML))
                    }
                    else -> {
                        // Short code block — keep it inline in the Markdown flow
                        // so MarkdownText can render it with syntax highlighting.
                        textBuffer.append(line).append('\n')
                        textBuffer.append(body).append('\n')
                        textBuffer.append(closingFenceLine).append('\n')
                    }
                }
                i = closeIdx + 1
                continue
            }

            // Bare HTML page (no fence) — detect inline `<!DOCTYPE…</html>`.
            val bareHtml = tryExtractBareHtml(lines, i)
            if (bareHtml != null) {
                flushText()
                segments.add(CardSegment.Tool(bareHtml.content, ToolType.HTML))
                i = bareHtml.nextLineIndex
                continue
            }

            textBuffer.append(line)
            if (i < lines.size - 1) textBuffer.append('\n')
            i++
        }

        flushText()
        return segments
    }

    /**
     * If [line] opens a fenced code block, returns the language tag (e.g.
     * "html", "python", "" for a bare fence). Returns null for non-fence lines.
     */
    private fun fenceLang(line: String): String? {
        val trimmed = line.trimStart()
        if (!trimmed.startsWith("```") && !trimmed.startsWith("~~~")) return null
        val tag = trimmed.substring(3).trim()
        return tag
    }

    private fun tryExtractBareHtml(lines: List<String>, startIdx: Int): BareHtml? {
        val start = lines[startIdx]
        // Heuristic: a bare HTML page begins with `<!DOCTYPE` or `<html`.
        val isPageStart = start.trimStart().lowercase().let {
            it.startsWith("<!doctype") || it.startsWith("<html")
        }
        if (!isPageStart) return null

        val sb = StringBuilder(start)
        var i = startIdx + 1
        while (i < lines.size) {
            sb.append('\n').append(lines[i])
            if (lines[i].trimEnd().lowercase().contains("</html>")) {
                return BareHtml(sb.toString(), i + 1)
            }
            i++
        }
        // No closing tag found — leave the original line alone; treat as text.
        return null
    }

    private data class BareHtml(val content: String, val nextLineIndex: Int)

    /**
     * Quick HTML-page heuristic. We require both `<html` and `</html>` markers
     * (case-insensitive) OR a `<!DOCTYPE` declaration. Plain `<div>…</div>`
     * fragments without `<html>` are NOT treated as tool cards — they're
     * already well-handled by MarkdownText.
     */
    fun looksLikeHtmlPage(content: String): Boolean {
        val lower = content.lowercase()
        val hasDoctype = lower.contains("<!doctype")
        val hasHtmlOpen = lower.contains("<html")
        val hasHtmlClose = lower.contains("</html>")
        return hasDoctype || (hasHtmlOpen && hasHtmlClose)
    }
}

/** A segment produced by [ToolCardRecognizer.split]. */
sealed class CardSegment {
    /**
     * Markdown text to render with the standard chat MarkdownText composable.
     * May contain short fenced code blocks (python/bash/sql/...) which render
     * inline with syntax highlighting.
     */
    data class Text(val markdown: String) : CardSegment()

    /**
     * A full-width tool card. The renderer wraps this in a [ToolCard]
     * composable with full-screen / collect / share buttons.
     */
    data class Tool(val content: String, val type: ToolType) : CardSegment()
}

enum class ToolType { HTML, MARKDOWN }
