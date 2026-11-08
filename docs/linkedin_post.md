More than half of the transit signal priority my loop detectors granted went to an empty bus lane.

I built an event-driven V2I testbed: J2735-shaped BSM/SRM messages → a pub/sub broker with radio latency and packet loss → roadside-unit logic for green extension and early green. I ran a median BRT corridor for 2 hours × 10 seeds under 9 strategies.

Bus signal delay per trip (No TSP: 29.9 s):
🔹 Loop detector TSP, 75-350 m setbacks: -14% to -24%
🔹 Connected TSP: -42%
🔹 Connected at 90% packet loss: still -38%

The finding I didn't expect: the setback distance mattered less than the station. With a near-side station inside the detection zone, the bus checks in, then dwells for 20 s, and the extension it earned expires. Buses used only 38-44% of loop-triggered priority. Connected TSP issues about as many requests but times them from the bus's own state, so 83% get used.

Other takeaways:
1️⃣ Redundancy beats link quality. A bus sends ~20 requests on approach and one is enough. The benefit only broke down at 97% loss. Repeat rate × request horizon is the real design parameter.
2️⃣ Reliability improves more than the average: travel-time SD dropped from 31.8 to 26.0 s.
3️⃣ The side street pays about +13% delay either way. Wasted priority costs exactly as much as useful priority, which is the strongest argument for better ETAs.

Systems side: the asyncio broker fans out 259k messages/s (p99 5 ms), a deterministic simulated-time channel makes every run reproducible, and there's a FastAPI ingest endpoint for binary frames.

Caveats: synthetic corridor; messages use J2735 fields and units but are struct-packed, not ASN.1 UPER.

Repo: [link]

Transit and signal folks: do you see near-side stops limiting TSP benefits in the field?

#TransitSignalPriority #ConnectedVehicles #V2X #BRT #TrafficEngineering #ITS #EventDriven #Python
