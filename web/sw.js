const CACHE='tradelab-v04-assets';
const ASSETS=['./','./index.html','./manifest.webmanifest','./icon-192.png','./icon-512.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS)));self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(names=>Promise.all(names.filter(n=>n!==CACHE).map(n=>caches.delete(n)))));self.clients.claim()});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.pathname.includes('/data/') || u.pathname.endsWith('full_report.html')) return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
