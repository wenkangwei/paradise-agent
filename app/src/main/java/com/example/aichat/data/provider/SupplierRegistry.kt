package com.example.aichat.data.provider

import javax.inject.Inject
import javax.inject.Singleton

/**
 * Read-only registry over [BuiltinSuppliers]. Feature code injects this to
 * look up a supplier by id without depending on the concrete object singletons.
 *
 * To add a runtime-registered supplier (e.g. fetched from a remote catalog in
 * the future), wrap `all` in a MutableStateFlow and call [register]; the API
 * surface is already prepared for it.
 */
interface SupplierRegistry {
    val all: List<Supplier>
    fun byId(id: String): Supplier?
    fun register(supplier: Supplier)
}

@Singleton
class DefaultSupplierRegistry @Inject constructor() : SupplierRegistry {
    @Volatile
    private var extras: List<Supplier> = emptyList()

    override val all: List<Supplier>
        get() = BuiltinSuppliers.all + extras

    override fun byId(id: String): Supplier? =
        BuiltinSuppliers.byId(id) ?: extras.firstOrNull { it.id == id }

    @Synchronized
    override fun register(supplier: Supplier) {
        if (all.none { it.id == supplier.id }) {
            extras = extras + supplier
        }
    }
}
