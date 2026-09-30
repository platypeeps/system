// Home (sd:2117): the design source's products/system/designs/v2/home-kiosk.js at d82daa1, unchanged. It runs in the head, so the wall
// display draws without the rail and pane from the first paint.
if (new URLSearchParams(location.search).get('kiosk') === '1') document.documentElement.setAttribute('data-kiosk', '');
