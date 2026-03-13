# Amazon Creators API — Valid SearchIndex Values (US Marketplace)

**Source:** Amazon PA-API 5.0 / Creators API Locale Reference
**Last verified:** 2026-03-13
**Marketplace:** www.amazon.com (US)

## SearchIndex Values

| SearchIndex | Category |
|---|---|
| All | All categories (default) |
| Apparel | Clothing |
| Appliances | Appliances |
| ArtsAndCrafts | Arts, Crafts & Sewing |
| Automotive | Automotive |
| Baby | Baby |
| Beauty | Beauty & Personal Care |
| Books | Books |
| Classical | Classical Music |
| Collectibles | Collectibles & Fine Art |
| DVD | DVD |
| DigitalMusic | Digital Music |
| Electronics | Electronics |
| FashionBaby | Baby Fashion |
| FashionBoys | Boys' Fashion |
| FashionGirls | Girls' Fashion |
| FashionMen | Men's Fashion |
| FashionWomen | Women's Fashion |
| GiftCards | Gift Cards |
| GourmetFood | Gourmet Food |
| Grocery | Grocery |
| HealthPersonalCare | Health & Personal Care |
| HomeGarden | Home & Garden |
| Industrial | Industrial & Scientific |
| Jewelry | Jewelry |
| KindleStore | Kindle Store |
| Kitchen | Kitchen & Dining |
| LawnAndGarden | Lawn & Garden |
| Magazines | Magazines |
| Marketplace | Marketplace |
| Miscellaneous | Miscellaneous |
| MobileApps | Mobile Apps |
| MP3Downloads | MP3 Downloads |
| Music | Music |
| MusicTracks | Music Tracks |
| MusicalInstruments | Musical Instruments |
| OfficeProducts | Office Products |
| OutdoorLiving | Outdoor Living |
| PCHardware | PC Hardware |
| PetSupplies | Pet Supplies |
| Photo | Camera & Photo |
| Shoes | Shoes |
| Software | Software |
| SportingGoods | Sporting Goods (legacy PA-API — may not work with Creators API) |
| SportsAndOutdoors | Sports & Outdoors (use this for Creators API) |
| Tools | Tools & Home Improvement |
| Toys | Toys & Games |
| UnboxVideo | Video |
| VHS | VHS |
| Video | Video |
| VideoGames | Video Games |
| Watches | Watches |
| Wireless | Cell Phones & Accessories |
| WirelessAccessories | Cell Phone Accessories |

## Notes

- If no SearchIndex is specified, the API defaults to `All`.
- SearchIndex values differ by marketplace. This list is for the US marketplace only.
- The Creators API may use different values than the legacy PA-API. For example, `SportsAndOutdoors` works with the Creators API while `SportingGoods` returns an "Invalid parameter value" error.
- Use `SearchRefinements` resource in your search request to discover relevant SearchIndexes dynamically.

## Current Usage in This Repo

| Pipeline | Config | SearchIndex |
|---|---|---|
| Weekly trending | `config/categories/womens-running-shoes.yaml` | FashionWomen |
| Softball | `config/softball/*.yaml` (all 9) | SportsAndOutdoors |
