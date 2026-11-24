import { createRouter, createWebHashHistory } from "vue-router";
import ProjectConfig from "./pages/ProjectConfig.vue";
import RunPage from "./pages/RunPage.vue";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/projects" },
    { path: "/projects", name: "projects", component: ProjectConfig },
    { path: "/runs/:id", name: "run", component: RunPage },
    { path: "/:pathMatch(.*)*", redirect: "/projects" },
  ],
});
