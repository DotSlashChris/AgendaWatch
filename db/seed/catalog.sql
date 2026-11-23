-- Reference catalog only (not used by the running app, which reads config.json).
-- No accounts, emails, watches, feedback, Stripe IDs, or alert history.
BEGIN;
CREATE TABLE IF NOT EXISTS ref_metros (
  state TEXT NOT NULL,
  metro TEXT NOT NULL,
  PRIMARY KEY (state, metro)
);
CREATE TABLE IF NOT EXISTS ref_cities (
  city_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  pmn_entity_name TEXT NOT NULL,
  state TEXT NOT NULL,
  metro TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_city_bodies (
  city_key TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  public_body TEXT NOT NULL,
  PRIMARY KEY (city_key, sort_order)
);
CREATE TABLE IF NOT EXISTS ref_keywords (
  sort_order INTEGER PRIMARY KEY,
  keyword TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS ref_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR REPLACE INTO ref_settings(key, value) VALUES('pmn_base', 'https://www.utah.gov');
INSERT OR REPLACE INTO ref_settings(key, value) VALUES('government_type_id_municipality', '3');
INSERT OR REPLACE INTO ref_settings(key, value) VALUES('lookback_days', '60');
INSERT OR REPLACE INTO ref_settings(key, value) VALUES('lookahead_days', '90');
INSERT OR REPLACE INTO ref_settings(key, value) VALUES('max_items_per_email', '5');
INSERT OR REPLACE INTO ref_settings(key, value) VALUES('alerts_off_cities', '[]');
INSERT OR REPLACE INTO ref_metros(state, metro) VALUES('Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('ch', 'Cottonwood Heights', 'Cottonwood Heights', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ch', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ch', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ch', 2, 'Board of Adjustments');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ch', 3, 'Administrative Hearings');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ch', 4, 'Architectural Review Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ch', 5, 'Appeals Hearing Officer');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('slc', 'Salt Lake City', 'Salt Lake City', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 0, 'Salt Lake City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 2, 'Historic Landmark Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 3, 'Planning Division Administrative Hearings');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 4, 'Housing Advisory and Appeals Board');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 5, 'Business Advisory Board');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 6, 'Board of Appeals and Examiners');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('slc', 7, 'Appeals Hearing Officer');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('sandy', 'Sandy', 'Sandy', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sandy', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sandy', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sandy', 2, 'Board of Adjustments');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sandy', 3, 'Community Development Department Administrative Hearing');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sandy', 4, 'Architectural Review Committee');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('draper', 'Draper', 'Draper', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('draper', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('draper', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('draper', 2, 'Board of Adjustments');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('draper', 3, 'Administrative Hearings');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('draper', 4, 'Historic Preservation Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('draper', 5, 'Zoning Administrator');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('wj', 'West Jordan', 'West Jordan', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('wj', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('wj', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('wj', 2, 'Board of Adjustment');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('wj', 3, 'Land Use Appeal Authority');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('wj', 4, 'Zoning Administrator');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('murray', 'Murray', 'Murray', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('murray', 0, 'Municipal Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('murray', 1, 'Planning and Zoning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('murray', 2, 'Hearing Officer Public Meeting');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('murray', 3, 'City Center Form Based Code ARC');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('murray', 4, 'Murray City Center District (MCCD) Design Review Committee');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('sj', 'South Jordan', 'South Jordan', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sj', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sj', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sj', 2, 'Board Of Adjustments');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sj', 3, 'VARIANCE AND APPEAL HEARINGS');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('sj', 4, 'ARCHITECTURAL REVIEW COMMITTEE');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('midvale', 'Midvale', 'Midvale', 'Utah', 'Salt Lake metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('midvale', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('midvale', 1, 'Planning Commission (Midvale)');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('midvale', 2, 'Board of Adjustments');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('midvale', 3, 'Appeal Authority');
INSERT OR REPLACE INTO ref_metros(state, metro) VALUES('Utah', 'Ogden-Clearfield metro');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('ogden', 'Ogden', 'Ogden', 'Utah', 'Ogden-Clearfield metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ogden', 0, 'City Council, Redevelopment Agency, Municipal Building Authority');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ogden', 1, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ogden', 2, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ogden', 3, 'Board of Zoning Adjustment');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('ogden', 4, 'Landmarks Commission');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('layton', 'Layton', 'Layton', 'Utah', 'Ogden-Clearfield metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('layton', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('layton', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('layton', 2, 'Board of Adjustment');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('layton', 3, 'Appeal Board');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('farmington', 'Farmington', 'Farmington', 'Utah', 'Ogden-Clearfield metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('farmington', 0, 'Farmington City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('farmington', 1, 'Farmington City Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('farmington', 2, 'Farmington City Board of Adjustment');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('farmington', 3, 'Historic Preservation Commission');
INSERT OR REPLACE INTO ref_metros(state, metro) VALUES('Utah', 'Provo-Orem metro');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('provo', 'Provo', 'Provo', 'Utah', 'Provo-Orem metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('provo', 0, 'Provo Municipal Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('provo', 1, 'Provo City Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('provo', 2, 'Planning Commission Administrative Hearing');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('provo', 3, 'Provo City Board of Adjustment');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('provo', 4, 'Provo City Landmarks Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('provo', 5, 'Provo City Design Review Committee');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('orem', 'Orem', 'Orem', 'Utah', 'Provo-Orem metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('orem', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('orem', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('orem', 2, 'Board of Adjustments');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('orem', 3, 'Development Review Committe');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('orem', 4, 'Historical Preservation Advisory Commission');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('vineyard', 'Vineyard', 'Vineyard', 'Utah', 'Provo-Orem metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('vineyard', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('vineyard', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('vineyard', 2, 'Development Review Committee');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('vineyard', 3, 'Administrative Law Judge/Hearing Officer');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('vineyard', 4, 'Public Notices');
INSERT OR REPLACE INTO ref_metros(state, metro) VALUES('Utah', 'Heber metro');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('daniel', 'Daniel', 'Daniel', 'Utah', 'Heber metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('daniel', 0, 'Town Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('daniel', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('daniel', 2, 'Board of Appeals');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('parkcity', 'Park City', 'Park City', 'Utah', 'Heber metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('parkcity', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('parkcity', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('parkcity', 2, 'Board of Adjustment');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('parkcity', 3, 'Administrative Public Hearing');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('parkcity', 4, 'Historic Preservation Board');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('parkcity', 5, 'Appeal Hearing');
INSERT OR REPLACE INTO ref_metros(state, metro) VALUES('Utah', 'St. George metro');
INSERT OR REPLACE INTO ref_cities(city_key, name, pmn_entity_name, state, metro) VALUES('stgeorge', 'St. George', 'St. George', 'Utah', 'St. George metro');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('stgeorge', 0, 'City Council');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('stgeorge', 1, 'Planning Commission');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('stgeorge', 2, 'Board of Adjustments');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('stgeorge', 3, 'Land Use Authority');
INSERT OR REPLACE INTO ref_city_bodies(city_key, sort_order, public_body) VALUES('stgeorge', 4, 'Historic Preservation Commission');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(0, 'short-term rental');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(1, 'STR');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(2, 'rental license');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(3, 'business license');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(4, 'zoning');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(5, 'conditional use');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(6, 'transient tax');
INSERT OR REPLACE INTO ref_keywords(sort_order, keyword) VALUES(7, 'occupancy');

-- Official city agenda page seeds (same rows aw_city_sources.ensure() inserts).
INSERT OR IGNORE INTO city_sources(city_key, url, added_at, added_by) VALUES(
  'ch',
  'https://www.cottonwoodheights.utah.gov/your-government/boards-and-commissions/planning-commission/agendas-packets-minutes',
  '2026-10-31T00:00:00-07:00',
  'seed: owner order 2026-10-31'
);
COMMIT;
