/* Tubarr mock data: made-up video titles by rough genre, and per-channel quirks for the preview.
   Only the mock uses this. Channel names/handles/subscriber counts come from seed.js (invented channels).
   Every title and description here is invented; game titles are fictional. */
(function () {
  'use strict';
  const MIN = 60e3, HOUR = 36e5, DAY = 864e5;
  const pick = (r, a) => a[Math.floor(r() * a.length)];
  /* ---------- Made-up words for the title templates ---------- */
  const W = {
    car: ['Station Wagon', 'Hatchback', 'Pickup', 'Roadster', 'Minivan', 'Coupe', 'Box Truck', 'Kei Truck', 'Muscle Car', 'Camper Van', 'Rally Car', 'Delivery Van', 'Convertible', 'Tow Truck'],
    miles: ['500', '1,000', '2,000', '3,000'],
    prod: ['Budget Laptop', 'Mini PC', 'Mesh Router', 'Mechanical Keyboard', 'OLED Monitor', 'Home NAS', 'Handheld PC', 'USB-C Dock', 'Smart Plug', 'Webcam', 'Portable SSD', 'E-Reader', 'Tablet', 'Soundbar', 'Label Printer'],
    prods: ['Power Banks', 'Case Fans', 'Budget SSDs', 'Cheap Routers', 'USB-C Hubs', 'Wireless Mice', 'Mini PCs', 'CPU Coolers', 'Surge Protectors', 'Desk Lamps'],
    count: ['8', '10', '12', '15', '20', '25'],
    x: ['Bubbles', 'Magnets', 'Lightning', 'Glass', 'Ice', 'Rainbows', 'Friction', 'Rust', 'Sound', 'Tides', 'Pendulums', 'Soap Films', 'Gears', 'Levers', 'Salt Crystals', 'Static Electricity', 'Echoes', 'Heat', 'Waves', 'Light'],
    sky: ['Comets', 'the Moon', 'Saturn’s Rings', 'Red Dwarfs', 'Meteor Showers', 'the Sun', 'Jupiter’s Storms', 'Nebulae', 'Exoplanets', 'the Milky Way'],
    place: ['the Coast Road', 'a Mountain Village', 'the Old Canal', 'a Night Market', 'the Salt Flats', 'a Lighthouse Island', 'the High Desert', 'a Fishing Town', 'the Lake District', 'a Border Town', 'the Fjords', 'a River Delta'],
    wood: ['Workbench', 'Dining Table', 'Bookshelf', 'Tool Chest', 'Rocking Chair', 'Cutting Board', 'Jewelry Box', 'Step Stool', 'Coat Rack', 'Garden Bench', 'Wall Clock', 'Picture Frame', 'Hand Plane', 'Mallet'],
    dish: ['Sourdough', 'Ramen', 'Dumplings', 'Lasagna', 'Flatbread', 'Pho', 'Curry', 'Pancakes', 'Cinnamon Rolls', 'Tamales', 'Paella', 'Croissants', 'Chili', 'Pierogi', 'Bagels'],
    plant: ['Tomatoes', 'Garlic', 'Peppers', 'Strawberries', 'Potatoes', 'Squash', 'Herbs', 'Beans', 'Lettuce', 'Blueberries', 'Sunflowers', 'Carrots'],
    console: ['8-Bit Console', '16-Bit Console', 'Handheld', 'Arcade Cabinet', 'Home Computer', 'Light Gun', 'Cartridge', 'CRT Television', 'Joystick', 'Memory Card'],
    game: ['Starfall Colony', 'Iron Frontier', 'Dungeon of Echoes', 'Hexfield', 'Skyport Tycoon', 'Moss Kingdom', 'Harbor Lines', 'Deepwell', 'Copper Crown', 'Ember Trail'],
    hist: ['the Salt Road', 'the Lighthouse Keepers', 'the Canal Builders', 'the Ice Trade', 'the First Telegraph', 'the Clockmakers’ Guild', 'the Wool Merchants', 'the Pony Riders', 'the Silver Mines', 'the Tea Clippers'],
    craft: ['Glider', 'Hot Air Balloon', 'Paraglider', 'Seaplane', 'Biplane', 'Airship'],
    weather: ['Thermals', 'Crosswinds', 'Sea Breezes', 'Mountain Waves', 'Fog', 'Thunderstorms'],
    mood: ['Cozy', 'Sleepy', 'Dreamy', 'Midnight', 'Calm', 'Focus'], season: ['Autumn', 'Winter', 'Spring', 'Summer'],
  };
  const GENRES = {
    cars: { cat: 'Autos & Vehicles', perWeek: [0.8, 3], dur: [12, 34], shorts: 0.2, live: 0.02, sb: 0, about: 'Old cars, cheap fixes and long drives. If it rolls, we will try to keep it rolling.',
      t: ['The {car} Build Is Finally Done', 'I Bought the Cheapest {car} I Could Find', 'Everything Broke on the {car} (Again)', 'Fixing Everything Wrong With My {car}', '{miles}-Mile Road Trip in a Junkyard {car}', 'First Drive in the {car} After {n} Months', 'Can We Save This Abandoned {car}?', 'Rust Repair on the {car}, Part {n}', 'Why Nobody Buys a {car} Anymore', 'The {car} vs the {car2}: Which Is Cheaper to Run?'],
      s: ['Cold start on the {car}', 'Did it break? Yes.'], l: ['Garage Night: Working on the {car}'] },
    tech: { cat: 'Science & Technology', perWeek: [1, 3.5], dur: [9, 28], shorts: 0.15, live: 0.04, sb: 0, about: 'Gadgets, home networking and computers: reviews, teardowns and honest buying advice.',
      t: ['{prod} Review: Worth It?', "Don't Buy a {prod} Yet", "{prod} Teardown: What's Inside?", "We Tested {count} {prods} So You Don't Have To", 'Building a Silent Home Server on a Budget', '{prod} vs {prod2}: Which One?', 'Fixing a Dead {prod}', 'The Worst {prods} We Have Ever Tested', 'Setting Up a {prod} the Right Way', 'My {year} Desk Setup'],
      s: ['This {prod} costs how much?', 'Never do this to a laptop'], l: ['LIVE: Ask a Tech Anything'] },
    science: { cat: 'Science & Technology', perWeek: [0.35, 1.2], dur: [10, 28], shorts: 0.18, live: 0.02, sb: 0, about: 'Everyday science explained with simple experiments, slow motion and a lot of curiosity.',
      t: ['The Strange Truth About {x}', 'What Nobody Tells You About {x}', 'The Surprising Physics of {x}', 'I Tried to Measure {x} at Home', 'The Problem With {x}', 'We Were Wrong About {x}', 'The Hidden Math Behind {x}', 'Making {x} From Scratch', 'A Simple Question About {x}', 'Why {x} Behave So Strangely'],
      s: ['{x} in slow motion', 'This should not work'], l: ['LIVE: Q&A About {x}'] },
    space: { cat: 'Science & Technology', perWeek: [0.4, 1.4], dur: [12, 30], shorts: 0.12, live: 0.05, sb: 0, about: 'Backyard astronomy and space news, explained without the jargon.',
      t: ['What We Just Learned About {sky}', 'How to See {sky} This Month', 'The Weirdest Thing About {sky}', 'A Year of Photographing {sky}', 'Why {sky} Are Harder to Study Than You Think', 'Night Sky Guide: {season} {year}'],
      s: ['{sky} tonight', 'One minute of stargazing'], l: ['LIVE: Telescope Night'] },
    travel: { cat: 'Travel & Events', perWeek: [0.5, 1.8], dur: [14, 38], shorts: 0.15, live: 0.02, sb: 0, about: 'Slow travel on a small budget: trains, buses, ferries and the places in between.',
      t: ['48 Hours in {place}', 'The Slowest Way to Reach {place}', 'Overnight Train to {place}', 'What $50 Buys You in {place}', 'Getting Lost in {place}', 'Why Nobody Visits {place}', 'Ferry Day: Crossing to {place}', 'Walking the Length of {place}'],
      s: ['Sunrise over {place}', 'The view from the train'], l: [] },
    woodworking: { cat: 'Howto & Style', perWeek: [0.3, 1.2], dur: [10, 26], shorts: 0.22, live: 0.02, sb: 0, about: 'Hand tools, a small shop and a lot of sawdust. Projects, techniques and honest mistakes.',
      t: ['Building a {wood} From One Board', 'The {wood} Is Finally Finished', 'Making a {wood} With Hand Tools Only', 'I Made a {wood} From Scrap Wood', 'Fixing My Worst {wood}', '{n} Joints Every Beginner Should Try', 'Restoring an Old {wood}', 'The Cheapest Way to Build a {wood}'],
      s: ['Perfect glue-up', '{wood} first coat of oil'], l: ['Live Shop Session: The {wood}'] },
    cooking: { cat: 'Howto & Style', perWeek: [0.8, 2.5], dur: [8, 24], shorts: 0.25, live: 0.02, sb: 0, about: 'Home cooking with simple ingredients, clear steps and the occasional kitchen disaster.',
      t: ['The Easiest {dish} You Will Ever Make', 'I Made {dish} Every Day for a Week', 'Restaurant {dish} at Home', '{dish} Three Ways', 'Fixing Your {dish}: {n} Common Mistakes', 'Budget {dish} for Four', 'Grandma-Style {dish}', 'Testing Viral {dish} Hacks'],
      s: ['{dish} in 30 seconds', 'Wait for the crunch'], l: ['LIVE: Cook Along, {dish} Night'] },
    gardening: { cat: 'Howto & Style', perWeek: [0.5, 1.5], dur: [9, 22], shorts: 0.2, live: 0, sb: 0, about: 'Growing food in small spaces: raised beds, containers, compost and a lot of trial and error.',
      t: ['Growing {plant} in Containers', 'Why My {plant} Failed This Year', 'The Easiest Way to Start {plant}', '{season} Garden Tour', 'Harvesting {plant}: Was It Worth It?', 'Compost That Actually Works', 'A Tiny Garden That Feeds a Family'],
      s: ['First {plant} of the season'], l: [] },
    history: { cat: 'Education', perWeek: [0.6, 2], dur: [12, 48], shorts: 0.12, live: 0, sb: 0, about: 'Stories about ordinary trades, forgotten routes and the small things that shaped history.',
      t: ['The Untold Story of {hist}', 'Why {hist} Disappeared', 'The Rise and Fall of {hist}', 'How {hist} Actually Worked', 'What Happened to {hist}?', 'A Day in the Life of {hist}', 'The Forgotten Rivals of {hist}'],
      s: ['The strangest object in the archive', 'This almost changed history'], l: [] },
    gaming: { cat: 'Gaming', perWeek: [2, 6], dur: [14, 45], shorts: 0.16, live: 0.16, sb: 0, about: "Let's plays, guides, big builds and live streams.",
      t: ['{game} Has Changed Forever', 'I Played {game} for 100 Days', '{game}: The Perfect Base', 'Is {game} Worth Playing in {year}?', 'The {game} Update Everyone Missed', '{game} Tier List (Updated)', 'Building a City in {game}, Episode {ep}'],
      s: ['{game} in 30 seconds', 'The rarest drop in {game}'], l: ['{game} Stream: Chill Night', 'LIVE: {game} Launch Day'] },
    retro: { cat: 'Gaming', perWeek: [0.5, 1.6], dur: [12, 34], shorts: 0.14, live: 0.04, sb: 0, about: 'Old consoles, flea-market finds and the repairs that bring them back to life.',
      t: ['Restoring a Yellowed {console}', 'I Found a {console} at a Flea Market', 'Fixing a Dead {console}', 'The {console} Nobody Remembers', 'Cleaning the Dirtiest {console} Yet', 'Is This {console} Worth Collecting?'],
      s: ['{console} first boot', 'Retro find of the week'], l: ['LIVE: Repair Bench'] },
    aviation: { cat: 'Education', perWeek: [0.4, 1], dur: [12, 26], shorts: 0.1, live: 0, sb: 0, about: 'Weather, small aircraft and the joy of quiet flying, explained for the curious.',
      t: ['Flying a {craft} Through {weather}', 'Why {weather} Matter to Every Pilot', 'A First Flight in a {craft}', 'Reading the Sky: {weather}', 'The {craft} That Almost Worked'],
      s: ['{weather} from above'], l: [] },
    music: { cat: 'Music', perWeek: [1, 2.5], dur: [55, 170], shorts: 0.08, live: 0.35, sb: 0, rate: 0.28, about: 'Calm beats to relax, study and focus to. A live radio around the clock and new mixes every week.',
      t: ['{mood} Beats Mix: {h} Hours', 'Late Night Tape Loops', 'Chill Essentials: {season} {year}', 'Rainy Day Beats', 'Deep Focus Mix: {h} Hours', 'Sunset Session', '{season} Mix', 'Beats to Work To, Vol. {n}'],
      s: ['New album out now'], l: ['calm radio: beats to relax to', 'night radio: beats to focus to'] },
    general: { cat: 'Entertainment', perWeek: [0.5, 2], dur: [8, 28], shorts: 0.25, live: 0.05, sb: 0, about: "Videos about whatever we're into this week. Thanks for watching.",
      t: ['Answering Your Questions', 'A Week Behind the Scenes', 'We Finally Did It', "Channel Update: What's Next", 'Road Trip Vlog, Part {n}', 'Studio Tour {year}', 'Our Biggest Project Yet'],
      s: ['Wait for it', 'Part 2?'], l: ['LIVE: Hang Out and Q&A'] },
  };
  function fill(tpl, r) {
    const used = {};
    return tpl.replace(/\{(\w+?)(2)?\}/g, (m, k, two) => {
      const list = Array.isArray(W[k]) ? W[k] : null;
      if (list) { let v = pick(r, list); if (two) { let g = 0; while (v === used[k] && g++ < 9) v = pick(r, list); } used[k] = v; return v; }
      if (k === 'n') return String(2 + Math.floor(r() * 7));
      if (k === 'h') return String(1 + Math.floor(r() * 3));
      if (k === 'ep') return String(1 + Math.floor(r() * 60));
      if (k === 'year') return '2026';
      return m;
    });
  }

  /* Four channels get hand-written titles, so their pages read naturally in screenshots. */
  const SPECIAL = {
    '@FieldNotesScience': { perWeek: 0.45, total: 412, rate: 1.25, settings: { retention: { mode: 'forever' } },
      about: 'Science stories told through simple experiments and the questions nobody thought to ask. New videos roughly every other week.',
      titles: ['Why Lightning Takes the Weirdest Path', 'The Paradox Hiding in Every Clock', 'Measuring the Speed of Sound in a Hallway', 'The Surprising Math of Traffic Jams', 'This Metal Remembers Its Shape', 'How to Build the Most Precise Ruler', 'What Nobody Tells You About Magnets', 'The Bizarre Physics of Spinning Tops', 'Why Bubbles Are Always Round', 'The Longest Experiment You Can Do at Home', 'Can You Hear a Color?', 'The Unsolved Problem Hiding in Your Kitchen', "Why Ice Is Slippery (It's Not What You Think)", 'Weighing a Mountain With a Pendulum'] },
    '@BenchmarkBarn': { perWeek: 4.5, total: 2950, rate: 1.15, settings: { sponsorblock: ['sponsor', 'selfpromo', 'interaction', 'outro'] },
      about: 'Long-term gadget reviews, noise and battery testing, and buying advice. Data first.',
      titles: ['Budget Laptop Review: One Year Later', 'The Loudest Mini PC We Have Ever Tested', 'How Cheap Chargers Are Made', 'Mesh Router Showdown: Six Brands Tested', 'Tech News: Prices, Recalls and Rumours', 'We Bought 12 Cheap Power Banks. Most Lied.', 'Portable SSD Long-Term Test: 12 Months In', 'Webcam Tier List, Retested', 'Fan Noise Marketing vs Reality: Measured', 'Handheld PC Review: Is It Finally Good?', 'The Truth About Fast Charging', 'Best Gadgets of the Year: Round-Up'] },
    '@SlowTrainJournal': { perWeek: 1.6, total: 640, rate: 1.6,
      about: 'Long train journeys filmed end to end: sleeper cars, dining cars, missed connections and the towns along the way. Weekly.',
      titles: ['Thirty Hours on the Slowest Sleeper Train', 'The Train That Only Runs on Tuesdays', 'Missed Connection: Stuck at a Mountain Station', 'Dining Car Review: Worth the Price?', 'Riding the Last Night Train of the Season', 'The Branch Line Nobody Uses', 'Crossing the Border by Rail', 'A Whole Day on Local Trains', 'The Most Scenic Hour of Rail We Have Filmed', 'First Class vs Economy on a Night Train', 'The Station That Is Also a Museum', 'Fourteen Tunnels in One Morning'] },
    '@CrookedDovetail': { perWeek: 0.32, total: 23, settings: { retention: { mode: 'forever' } },
      about: 'Crooked Dovetail: hand-tool woodworking in a one-car garage. Slow projects, honest mistakes.',
      titles: ['Flattening a Warped Board by Hand', 'My First Set of Hand-Cut Dovetails', 'A Workbench From Reclaimed Pine', 'Cheap Hand Plane: Worth Tuning Up?', 'Sharpening Chisels Without Fancy Stones', 'Garage Shop Tour 2026', 'Fixing a Wobbly Kitchen Chair', 'Oil vs Wax: A Six-Month Test', 'Building a Tool Tote for Beginners', 'The Step Stool Gets a New Top'] },
  };
  /* Overrides on top of fill-and-roll, to show every kind in the preview. */
  const OVERRIDES = {
    '@PocketGadgetDaily': { perWeek: 7, settings: { retention: { mode: 'count', count: 25 } } },
    '@RespawnRadio': { perWeek: 9, settings: { retention: { mode: 'new_only' }, max_duration_minutes: 90, quality: '720p' } },
    '@VelvetTapeLoops': { settings: { quality: '720p', sponsorblock: [], retention: { mode: 'days', days: 30 } } },
    '@LowOrbitLog': { settings: { retention: { mode: 'since', since: '2025-01-01' } }, sync: { done: null, total: null, ms: 12e3, refresh: true } },
    '@PantryScience': { total: 38, settings: { retention: { mode: 'forever' } } },
    '@BridgeMath': { total: 180, settings: { retention: { mode: 'forever' } } },
    '@HexfieldPlays': { settings: { include_live: true } },
    '@LedgerOfEmpires': { settings: { max_duration_minutes: 120 } },
    '@SourdoughLab': { settings: { retention: { mode: 'count', count: 15 } }, addedAgo: 20 * HOUR },
    '@TerraceGardenDiary': { addedAgo: 9 * DAY },
    '@WeekendWrenching': { addedAgo: 6 * MIN, sync: { done: 64, total: 212, ms: 80e3 }, total: 212 },
    '@PaperLanternCrafts': { addedAgo: 2 * MIN, sync: { done: 3, total: 14, ms: 22e3 }, total: 14 },
  };
  /* Plex organization: one topic per channel (a guess from the genre, a few set by hand), and playlists detected as series. */
  const GENRE_TOPIC = { cars: 'cars', tech: 'tech', science: 'science', space: 'space', aviation: 'aviation', travel: 'other', woodworking: 'makers', gardening: 'makers', cooking: 'other', history: 'history', gaming: 'gaming', retro: 'gaming', music: 'music', general: 'other' };
  const TOPIC_OVERRIDE = { '@ScrapyardRobotics': 'makers', '@KitchenChemistryHour': 'science', '@PantryScience': 'science', '@PaperLanternCrafts': 'makers' };
  const SERIES = {
    '@PixelHarvest': { series: [['Console Restoration Diaries', 14, 'playlist'], ['The Flea Market Challenge', 22, 'playlist']], ignored: [['Best of 2025', 22, 'compilation'], ['Shorts', 61, 'shorts']] },
    '@HexfieldPlays': { series: [['Starfall Colony: Zero to Megabase', 30, 'playlist']], ignored: [['Beginner guides', 18, 'not_numbered'], ['Stream archive', 120, 'live']] },
    '@CrookedDovetail': { series: [['Workbench From Scratch', 6, 'upload']], ignored: [['Tool reviews', 4, 'not_numbered']] },
    '@RustToRoad': { series: [['Project Wagon Revival', 8, 'playlist']], ignored: [['Autumn Car Show 2026', 31, 'mixed_channels']] },
    '@CartridgeCove': { series: [['Dungeon of Echoes: Blind Run', 18, 'playlist']], ignored: [] },
    '@MaproomTactics': { series: [['Iron Frontier: Hardcore Campaign', 12, 'playlist']], ignored: [['Quick tips', 2, 'too_small']] },
    '@FieldNotesScience': { series: [], ignored: [['Best of Field Notes', 40, 'compilation']] },
  };
  const PART_SUBTITLES = ['The Beginning', 'First Steps', 'Scaling Up', 'A New Plan', 'A Costly Mistake', 'Rebuilding', 'The Big Expansion', 'Small Wins', 'New Frontier', 'Tidying Everything', 'The Final Push', 'Unexpected Problems', 'The Big One', 'Finishing Touches'];
  window.TUBARR_MOCK_DATA = { W, GENRES, fill, SPECIAL, OVERRIDES, GENRE_TOPIC, TOPIC_OVERRIDE, SERIES, PART_SUBTITLES };
})();
