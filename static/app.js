/* app.js — minimal global helpers */

// Auto-refresh dashboard cluster status every 60 seconds
if (document.querySelector('.dashboard')) {
  setInterval(async () => {
    try {
      const r    = await fetch('/api/cluster');
      const data = await r.json();
      for (const node of data.nodes) {
        const cards = document.querySelectorAll('.node-card');
        for (const card of cards) {
          if (card.querySelector('.node-name')?.textContent === node.name) {
            card.classList.toggle('node-up',   node.up);
            card.classList.toggle('node-down', !node.up);
          }
        }
      }
    } catch(e) {}
  }, 60000);
}
