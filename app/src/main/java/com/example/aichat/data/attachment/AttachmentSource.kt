package com.example.aichat.data.attachment

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CameraAlt
import androidx.compose.material.icons.filled.InsertDriveFile
import androidx.compose.material.icons.filled.PhotoLibrary
import androidx.compose.ui.graphics.vector.ImageVector

/**
 * Identifies a single attachment origin that the user can pick from the ⊕ sheet.
 *
 * Why an enum-like sealed interface instead of just ints: a future plugin could
 * contribute additional sources (e.g. "Audio recording", "Cloud drive") by
 * registering them with [AttachmentSourceRegistry] — the UI iterates the
 * registry and renders a row per source without hardcoded branching.
 *
 * The actual launcher contract is owned by the Compose layer because
 * `rememberLauncherForActivityResult` must be called from a @Composable scope;
 * each [Kind] tells the composable which contract to register.
 */
sealed interface AttachmentSource {
    val id: String
    val label: String
    val icon: ImageVector

    /** Camera capture. Launched via `ActivityResultContracts.TakePicture`. */
    data object Camera : AttachmentSource {
        override val id = "camera"
        override val label = "拍照"
        override val icon: ImageVector = Icons.Filled.CameraAlt
    }

    /** Gallery picker (images/videos). Launched via `PickVisualMedia`. */
    data object Gallery : AttachmentSource {
        override val id = "gallery"
        override val label = "相册"
        override val icon: ImageVector = Icons.Filled.PhotoLibrary
    }

    /** Generic file picker. Launched via `GetContent`. */
    data object Files : AttachmentSource {
        override val id = "files"
        override val label = "文件"
        override val icon: ImageVector = Icons.Filled.InsertDriveFile
    }

    /** OCR text recognition via ML Kit. */
    data object Ocr : AttachmentSource {
        override val id = "ocr"
        override val label = "文字识别"
        override val icon: ImageVector = Icons.Filled.TextSnippet
    }

    /** Object detection via YOLO (server-side). */
    data object ObjectDetection : AttachmentSource {
        override val id = "detect"
        override val label = "物体检测"
        override val icon: ImageVector = Icons.Filled.Visibility
    }
}

/**
 * Catalog of available [AttachmentSource]s. Concrete sources are enumerated here;
 * runtime registration is supported (mirrors [com.example.aichat.data.provider.SupplierRegistry])
 * so future plugins can append sources without touching the UI.
 */
class AttachmentSourceRegistry {
    @Volatile
    private var extras: List<AttachmentSource> = emptyList()

    val all: List<AttachmentSource>
        get() = listOf(AttachmentSource.Camera, AttachmentSource.Gallery, AttachmentSource.Files,
            AttachmentSource.Ocr, AttachmentSource.ObjectDetection) + extras

    fun byId(id: String): AttachmentSource? = all.firstOrNull { it.id == id }

    @Synchronized
    fun register(source: AttachmentSource) {
        if (all.none { it.id == source.id }) {
            extras = extras + source
        }
    }
}
