/* Shared light/dark theme preference and navbar control. */
(function () {
  const storageKey = 'openhire_theme';
  let storedTheme = null;
  try {
    storedTheme = localStorage.getItem(storageKey);
  } catch (error) {
    // Keep the page usable when browser storage is unavailable.
  }
  const initialTheme = storedTheme === 'dark' || storedTheme === 'light'
    ? storedTheme
    : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

  document.documentElement.dataset.theme = initialTheme;

  function updateThemeToggle() {
    const isDark = document.documentElement.dataset.theme === 'dark';
    document.querySelectorAll('[data-theme-toggle]').forEach((toggle) => {
      toggle.setAttribute('aria-pressed', String(isDark));
      toggle.setAttribute('aria-label', isDark ? 'Switch to light theme' : 'Switch to dark theme');
      toggle.setAttribute('title', isDark ? 'Switch to light theme' : 'Switch to dark theme');
      const icon = toggle.querySelector('i');
      if (icon) {
        icon.className = isDark ? 'ph-bold ph-sun' : 'ph-bold ph-moon';
      }
    });
  }

  window.toggleTheme = function () {
    const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = nextTheme;
    try {
      localStorage.setItem(storageKey, nextTheme);
    } catch (error) {
      // The selected theme still applies for the current page.
    }
    updateThemeToggle();
  };

  window.updateThemeToggle = updateThemeToggle;
  document.addEventListener('DOMContentLoaded', updateThemeToggle);
}());
