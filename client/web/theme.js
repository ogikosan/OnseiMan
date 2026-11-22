// 前回選んだ色（ライト／ダーク）を、画面を描く前に反映する
try { const t = localStorage.getItem('onseiman.theme'); if (t) document.documentElement.dataset.theme = t; } catch {}
