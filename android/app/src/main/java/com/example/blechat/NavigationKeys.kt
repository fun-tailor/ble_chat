package com.example.blechat

import androidx.navigation3.runtime.NavKey
import kotlinx.serialization.Serializable

@Serializable data object Chat : NavKey
@Serializable data object Join : NavKey
@Serializable data object History : NavKey
@Serializable data object Settings : NavKey
