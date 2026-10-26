// One source-generated table; filtering changes visibility, never its capability claims.
const root = document.documentElement;
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
let appearance = 'system';
try { const saved=localStorage.getItem('xp-manual-theme'); if(['system','light','dark'].includes(saved)) appearance=saved; } catch { /* No storage is required to read the chart. */ }
function renderTheme() {
 root.dataset.appearance=appearance;
 const actual=appearance==='system'?(systemTheme.matches?'dark':'light'):appearance;
 const next={system:'light',light:'dark',dark:'system'}[appearance];
 document.querySelectorAll('.theme').forEach(button=>{button.textContent=`◐ ${appearance==='system'?'System · '+actual:appearance[0].toUpperCase()+appearance.slice(1)}`;button.setAttribute('aria-label',`Appearance: ${appearance}. Switch to ${next}.`);});
}
renderTheme();
systemTheme.addEventListener('change',renderTheme);
document.querySelectorAll('.theme').forEach(button=>button.addEventListener('click',()=>{appearance={system:'light',light:'dark',dark:'system'}[appearance];try{localStorage.setItem('xp-manual-theme',appearance);}catch{/* Visit-only preference. */}renderTheme();}));
const filters=[...document.querySelectorAll('[data-group-filter]')];
const rows=[...document.querySelectorAll('[data-product]')];
function showGroup(group) {
 filters.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.groupFilter===group)));
 let count=0;
 rows.forEach(row=>{row.hidden=group!=='all'&&row.dataset.group!==group&&row.id!=='xperfect';if(!row.hidden)count++;});
 document.querySelector('.filter-count').textContent=`${count} products · xPerfect included in every category`;
}
filters.forEach(button=>button.addEventListener('click',()=>showGroup(button.dataset.groupFilter)));
document.getElementById('show-mechanisms').addEventListener('change',event=>document.body.classList.toggle('show-mechanisms',event.target.checked));
