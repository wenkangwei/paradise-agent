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
 *
 * v4.2.12 #4: also injects `<base href="https://aichat.local/">` when no
 * `<base>` tag is present. Combined with switching the WebView's baseURL
 * from `about:blank` to `https://aichat.local/`, this gives LLM-generated
 * pages a stable, non-null origin so:
 *   - JS sees `window.location.origin === "https://aichat.local"` (some
 *     libraries and iframe sandboxes error out under null origin).
 *   - Relative URLs (`<img src="foo.png">`, `fetch("/api/x")`) resolve
 *     against a synthetic origin instead of `about:blank` (which always
 *     404s and breaks CORS-inward checks).
 *   - localStorage / IndexedDB have a legitimate scope rather than the
 *     ephemeral about:blank storage that's wiped per session.
 *
 * The `aichat.local` host doesn't actually serve anything — relative
 * resources still 404 in practice — but the *origin semantics* in JS
 * land are correct, which is what most libraries care about.
 */
object HtmlViewport {

    private const val SYNTHETIC_ORIGIN = "https://aichat.local/"

    private val VIEWPORT_META = """
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes">
    """.trimIndent()

    private val BASE_TAG = "<base href=\"$SYNTHETIC_ORIGIN\">"

    /**
     * Returns [html] with a viewport meta tag injected into `<head>`
     * unless one is already present. Pages without a `<head>` get a
     * synthetic head tag inserted right after `<html>` (or prepended
     * altogether if even `<html>` is missing). A `<base>` tag is also
     * injected so relative URLs resolve against [SYNTHETIC_ORIGIN].
     */
    fun ensureMobileViewport(html: String): String {
        val alreadyHasViewport = html.contains("name=\"viewport\"", ignoreCase = true) ||
            html.contains("name='viewport'", ignoreCase = true)
        val alreadyHasBase = Regex("<base\\b", RegexOption.IGNORE_CASE).containsMatchIn(html)

        // Fast path: nothing to add.
        if (alreadyHasViewport && alreadyHasBase) return html

        val injectables = buildString {
            if (!alreadyHasViewport) append(VIEWPORT_META)
            if (!alreadyHasBase) append(BASE_TAG)
        }

        // Insert into existing <head> if we have one.
        val headOpen = Regex("<head\\b[^>]*>", RegexOption.IGNORE_CASE).find(html)
        if (headOpen != null) {
            val insertAt = headOpen.range.last + 1
            return html.substring(0, insertAt) + injectables + html.substring(insertAt)
        }
        // Otherwise inject right after the opening <html> tag; create both
        // if missing.
        val htmlOpen = Regex("<html\\b[^>]*>", RegexOption.IGNORE_CASE).find(html)
        if (htmlOpen != null) {
            val insertAt = htmlOpen.range.last + 1
            return html.substring(0, insertAt) +
                "<head>$injectables</head>" +
                html.substring(insertAt)
        }
        // Bare HTML fragment — wrap minimally so the WebView sees a normal
        // document structure and respects the viewport.
        return "<!DOCTYPE html><html><head>$injectables</head><body>$html</body></html>"
    }
}
