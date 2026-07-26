package com.example.aichat.ui.chat.toolcard

/**
 * Preprocesses LLM-generated HTML before handing it to WebView.
 *
 * v4.2.4 fix for "HTML preview can scroll, but the page is rendered
 * cut-off / can't see the whole thing like a real browser":
 *
 * Most LLM-generated HTML pages omit the `<meta name="viewport">` tag.
 * Without it, the WebView falls back to a default ~980px CSS viewport and
 * then `loadWithOverviewMode = true` zooms the entire page out so it fits
 * the device screen width. Visually this looks like the page is rendered
 * at 30% zoom and "scrolling" doesn't reveal more content — the user
 * sees a shrunk whole page instead of a normal browser layout.
 *
 * Injecting `<meta name="viewport" content="width=device-width,
 * initial-scale=1.0">` into `<head>` makes the WebView lay the page out
 * at the device's actual pixel width, exactly like Chrome / Firefox do
 * on mobile. Long content then scrolls vertically as expected.
 *
 * If the page already declares its own viewport, the LLM's intent wins.
 */
object HtmlViewport {

    private val VIEWPORT_META = """
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes">
    """.trimIndent()

    /**
     * Returns [html] with a viewport meta tag injected into `<head>`
     * unless one is already present. Pages without a `<head>` get a
     * synthetic head tag inserted right after `<html>` (or prepended
     * altogether if even `<html>` is missing).
     */
    fun ensureMobileViewport(html: String): String {
        if (html.contains("name=\"viewport\"", ignoreCase = true) ||
            html.contains("name='viewport'", ignoreCase = true)) {
            return html
        }
        // Insert into existing <head> if we have one.
        val headOpen = Regex("<head\\b[^>]*>", RegexOption.IGNORE_CASE).find(html)
        if (headOpen != null) {
            val insertAt = headOpen.range.last + 1
            return html.substring(0, insertAt) + VIEWPORT_META + html.substring(insertAt)
        }
        // Otherwise inject right after the opening <html> tag; create both
        // if missing.
        val htmlOpen = Regex("<html\\b[^>]*>", RegexOption.IGNORE_CASE).find(html)
        if (htmlOpen != null) {
            val insertAt = htmlOpen.range.last + 1
            return html.substring(0, insertAt) +
                "<head>$VIEWPORT_META</head>" +
                html.substring(insertAt)
        }
        // Bare HTML fragment — wrap minimally so the WebView sees a normal
        // document structure and respects the viewport.
        return "<!DOCTYPE html><html><head>$VIEWPORT_META</head><body>$html</body></html>"
    }
}
