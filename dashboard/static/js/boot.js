"use strict";
/* Boot: wire every view, open the one the address asks for, start the transport. */
wireShell();
wireOps();
wireAgent();
wireDecide();
wireImage();
wireVideo();
showView(location.hash.slice(1) || 'now', false);
startTransport();
