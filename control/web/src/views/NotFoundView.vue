<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <header class="page-heading">
    <h1>页面未找到</h1>
  </header>
  <RouterLink class="text-link" :to="{ name: 'overview' }">返回概览</RouterLink>
</template>
