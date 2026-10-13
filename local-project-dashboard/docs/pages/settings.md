# Settings

Settings is at `/settings` (sd:3438): what this browser keeps for the
dashboard. The theme is Dark, Light or System (`theme.js`); Dark is the
default. The sections rail is Narrow, Default, Wide or Widest, or any width
from 176 to 384 px dragged at its edge (`shell.railWidth`, sd:3445). A change
saves at once and says Saved. Both live in the browser's storage, so the page
has no API route, writes nothing on the server, and another browser keeps its
own.
