#!/usr/bin/env python

import datetime
import os
import re
import subprocess
import sys
import time
import wikipedia

from duckduckgo_search import DDGS
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import moses_common.__init__ as common
import moses_common.collective
import moses_common.dynamodb
import moses_common.s3
import moses_common.stabilityai
import moses_common.ui
import moses_common.visual_artists as visual_artists


ui = moses_common.ui.Interface(use_slack_format=True, usage_message="""
Manage images and data for Art Intelligence.
  manage.py astats                   # Display category stats on artists db
  manage.py deploy                   # Copy dev S3 website to prod S3
  manage.py fail                     # Send local fail website to S3
  manage.py import <filename>        # Import images from image_generator.py
  manage.py new_artist <artist name> # Add new artist and default work record
  manage.py refresh                  # Grab new artist file and sync to db
  manage.py send                     # Send local dev website to S3
  manage.py stats                    # Display category stats on image db
  manage.py update_url [url]         # Update artist URL and bio
  manage.py artist_db                # Update artist db - requires editing
  manage.py image_db                 # Update image db - requires editing
  
  Options:
    -h, --help                  This help screen.
    -v, --verbose               More output.
    -x, --extra_verbose         Even more output.
""")


def handler(args, opts):
	success = False
	response = None
	action = args['action']
	
	dry_run_arg = ''
	if dry_run:
		dry_run_arg = ' --dryrun'
	
	if action == 'astats':
		success, response = get_artist_stats(opts)
	elif action == 'deploy':
		subprocess.run(f"aws s3 sync{dry_run_arg} --delete --exclude critique.html --exclude manage.html s3://artintelligence.gallery/dev s3://artintelligence.gallery/web", shell=True)
	elif action == 'engines':
		success, response = get_engine_list()
	elif action == 'fail':
		os.chdir("/Users/tim/Repositories/art-intelligence")
		subprocess.run(f"aws s3 sync{dry_run_arg} --delete fail s3://artintelligence.gallery/fail", shell=True)
# 	elif action == 'import':
# 		success, response = import_image(args['file'], opts)
	elif action == 'refresh':
		success, response = refresh_artists(opts)
	elif action == 'reload':
		success, response = reload_db(args['target'])
	elif action == 'send':
		os.chdir("/Users/tim/Repositories/art-intelligence")
		subprocess.run(f"aws s3 sync{dry_run_arg} --delete dev s3://artintelligence.gallery/dev", shell=True)
	elif action == 'stats':
		success, response = get_genre_stats(opts)
	elif action == 'artist_db':
		success, response = update_artist_records(opts)
	elif action == 'new_artist':
		success, response = new_artist(args, opts)
	elif action == 'collective_db':
		success, response = update_collective_records(opts)
	elif action == 'works_db':
		success, response = update_work_records(opts)
	elif action == 'image_db':
		success, response = update_image_records(opts)
	elif action == 'search':
		success, response = search(args, opts)
	elif action == 'test':
		success, response = test(opts)
	elif action == 'update_url':
		success, response = update_url(args, opts)
	
	elif action == 'all_methods':
		collective = moses_common.collective.Collective(log_level=log_level, dry_run=dry_run)
		response = collective.get_all_methods()
		success = True
	
	else:
		response = "Invalid command"
	
	if not success:
		if response:
			ui.error(response)
		sys.exit(2)
	if response:
		ui.success("Success")
		if type(response) is dict or type(response) is list:
			ui.pretty(response)
		else:
			print(response)


def get_artist_stats(opts):
	collective = get_collective()
	artists = collective.get_artists()
	
	art_forms = collective.get_art_forms()
	centuries, subjects, styles = collective.get_categories()	
	
	stats = {
		"art_forms": {},
		"methods": {},
		"subjects": {},
		"no_art_form": 0,
		"no_method": 0,
		"no_subject": 0
	}
	for artist in artists:
		art_form = False
		method = False
		subject = False
		for cat in artist.categories:
			if cat in art_forms:
				if cat not in stats['art_forms']:
					stats['art_forms'][cat] = 0
				stats['art_forms'][cat] += 1
				art_form = True
				if 'methods' in art_forms[cat]:
					for inner_cat in artist.categories:
						if inner_cat in art_forms[cat]['methods']:
							if cat not in stats['methods']:
								stats['methods'][cat] = 0
							stats['methods'][cat] += 1
							method = True
			if cat in subjects:
				if cat not in stats['subjects']:
					stats['subjects'][cat] = 0
				stats['subjects'][cat] += 1
				subject = True
		if not art_form:
			stats['no_art_form'] += 1
		if not method:
			stats['no_method'] += 1
		if not subject:
			stats['no_subject'] += 1
			
	return True, stats

def get_engine_list():
	stabilityai = moses_common.stabilityai.StableDiffusion(
		log_level = log_level,
		dry_run = dry_run
	)
	response = stabilityai.get_engine_list()
	return True, response


def import_image(files, opts):
	if not len(files):
		return False, "No files specified"
	
	success = False
	updated_cnt = 0
	for file in files:
		ui.header(os.path.basename(file))
		
		success, image_changed, record = fix_image(file)
		if not success:
			ui.error(record)
			continue
		
		inner_success, response = send_image(file, record, image_changed=image_changed)
		if inner_success:
			success = True
			updated_cnt += 1
		else:
			ui.warning(response)
		
		if opts['delete']:
			os.remove(file)
			ui.warning("Deleted " + file)
	
	if not success:
		return False, ""
	return True, "  Added: " + ui.format_text(updated_cnt, 'bold')


def send_image(filename, data, image_changed=False):
	flat_data = common.flatten_hash(data)
	table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
	
	bucket_name = 'artintelligence.gallery'
	object_name = f"images/{data['filename']}"
	bucket = moses_common.s3.Bucket(bucket_name)
	file = moses_common.s3.Object(bucket, object_name)
	
	dd_record = table.get_item(data['filename'], data['create_time'])
	if dd_record:
		if image_changed:
			ui.body("  Uploading changed image...")
			response = file.upload_file(filename)
		return False, "Image is already uploaded"
	
	response = file.upload_file(filename)
	
	data['image_url'] = f"https://{bucket_name}/{object_name}"
	
	ui.body("  Adding new image...")
	table.put_item(data)
	return True, data['image_url']


def fix_image(file):
	collective = get_collective()
	image = Image.open(file)
	record = image.info
	if 'prompt' not in record:
		return False, False, "Missing prompt data"	
	
	# Add fields
	should_update = False
	if 'filename' not in record or not record['filename'] or record['filename'] == "None":
		should_update = True
		record['filename'] = os.path.basename(file)
	
	name_record = get_data_from_filename(record['filename'])
	if 'query-artist' not in record and 'artist_name' in name_record:
		record['query-artist'] = name_record['artist_name']
	artist = None
	if 'query-artist' in record:
		artist = collective.get_artist(record['query-artist'])
		if not artist:
			ui.pretty(record)
	
	if 'create_time' not in record:
		record['create_time'] = name_record['create_time']
	if 'engine_name' not in record:
		record['engine_name'] = name_record['engine_name']
	if 'engine_label' not in record:
		record['engine_label'] = name_record['engine_label']
	
	if 'query-model' not in record and artist:
		record['query-model'] = artist.checkpoint
	if 'model' not in record and 'engine' in record:
		record['model'] = "Deliberate V2"
		record['model_id'] = "K6KkkKl"
		if re.match(r'dreamshaper', record['engine'], re.IGNORECASE):
			record['model'] = "DreamShaper"
			record['model_id'] = "4zdwGOB"
			record['model_version'] = "5"
	if 'engine' in record:
		del(record['engine'])
	
	if 'width' not in record:
		record['width'] = image.width
	if 'height' not in record:
		record['height'] = image.height
	
	if 'orientation' not in record or 'aspect' not in record:
		record['orientation'] = 'square'
		record['aspect'] = 'square'
		longest = 512
		if int(record['width']) > int(record['height']):
			record['orientation'] = 'landscape'
			longest = int(record['width'])
		elif int(record['width']) < int(record['height']):
			record['orientation'] = 'portrait'
			longest = int(record['height'])
		if longest == 640:
			record['aspect'] = 'full'
		elif longest == 768:
			record['aspect'] = '35'
		elif longest == 896:
			record['aspect'] = 'hd'
			
	
	# Check for missing fields
	missing = []
	required = ['filename', 'create_time', 'engine_label', 'engine_name', 'prompt', 'seed', 'steps', 'cfg_scale', 'width', 'height', 'negative_prompt', 'orientation', 'aspect']
	if record['engine_name'] == 'sinkin':
		required = required + ['model', 'model_id']
	for field in required:
		if field not in record or not record[field]:
			missing.append(field)
	if len(missing):
		if log_level >= 7:
			ui.pretty(record)
		return False, False, "Missing data fields: {}".format(', '.join(missing))
	
	
	# Convert type
	for number in ['seed', 'steps', 'width', 'height']:
		record[number] = common.convert_to_int(record[number])
	record['cfg_scale'] = common.convert_to_float(record['cfg_scale'])
	
	
	# Fix fields
	if record['engine_name'] == 'sinkin':
		artist = collective.get_artist(record['query-artist'])
		if record['query-model'] != artist.checkpoint:
			should_update = True
			ui.body(f"  query-model is '{record['query-model']}' but should be '{artist.checkpoint}'")
			record['query-model'] = artist.checkpoint
	
	
	# Update file
	if should_update:
		ui.body("  Updating image data...")
		image.save(file, pnginfo=get_png_info(record))
		return True, True, record
	
	return True, False, record


def get_data_from_filename(file):
	parts = file.split('-')
	if log_level >= 7:
		print(f"Name parts: {parts}")
	
	record = {}
	
	if not common.is_int(parts[0]):
		artist = parts.pop(0)
		artist = re.sub(r'_', ' ', artist)
		record['artist_name'] = artist.title()
	
	record["create_time"] = datetime.datetime.fromtimestamp(int(parts[0])).isoformat(' ')
	record["engine_label"] = "sinkin.ai"
	record["engine_name"] = "sinkin"
	
	if re.match(r'sd', parts[1]):
		record["engine_label"] = get_full_model_name(parts[1])
		record["engine_name"] = parts[1]
	elif parts[1] == 'dalle':
		record["engine_label"] = "DALL·E"
		record["engine_name"] = "dalle"
	
	if len(parts) > 3:
		record['seed'] = re.sub(r'\.png', '', parts[2])
	
	if log_level >= 7:
		ui.body("Data from filename:")
		ui.pretty(record)
	return record


def refresh_artists(opts):
	if log_level >= 6:
		ui.header("Sync artists database")
	collective = get_collective()
	updated_cnt = collective.sync_artists_to_db()
	
	return True, "Updates: " + ui.format_text(updated_cnt, 'bold')

def reload_db(target):
	collective = moses_common.collective.Collective(log_level=log_level, dry_run=dry_run)
	if target in ['artist', 'artists']:
		collective.set_artists_update()
		return True, f"Set {target} to reload"
	elif target in ['genre', 'genres', 'work', 'works']:
		collective.set_genres_update()
		return True, f"Set {target} to reload"
	elif target in ['image', 'images']:
		collective.set_images_update()
		return True, f"Set {target} to reload"
	return False, "Unknown target db"

def get_genre_stats(opts):
	genre_table = moses_common.dynamodb.Table('artintelligence.gallery-works', log_level=log_level, dry_run=dry_run)
	genre_records = genre_table.get_keys()
	
	stats = {}
	for genre in genre_records:
		if genre['name'] not in stats:
			stats[genre['name']] = 1
		else:
			stats[genre['name']] += 1
	
	sorted_genres = sorted(stats.keys())
	genre_list = []
	for genre in sorted_genres:
		genre_list.append(f"{stats[genre]}:{genre}")
	return True, genre_list


def get_image_stats(opts):
	image_table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
	image_records = image_table.get_keys(['query-art_form', 'query-method', 'query-style', 'query-subject'])
	
	stats = {}
	for image in image_records:
		for field in ['art_form', 'method', 'style', 'subject']:
			if "query-" + field in image:
				if field not in stats:
					stats[field] = {}
				if image["query-" + field] not in stats[field]:
					stats[field][image["query-" + field]] = 0
				stats[field][image["query-" + field]] += 1
	return True, stats


def update_artist_records(opts):
	table = moses_common.dynamodb.Table('artintelligence.gallery-artists', log_level=log_level, dry_run=dry_run)
	records = table.scan()
	
	cnt = 0
	updated_cnt = 0
	total = len(records)
	for record in records:
		if opts['limit'] and opts['limit'] <= cnt:
			break
		cnt += 1
		should_update = False
		ui.header(record['id'])
		updated_record = {
			"id": record['id']
		}
		
		
		# Start manipulate record
		
		if 'preferred_model' not in record:
			updated_record['preferred_model'] = 'sdxl'
			should_update = True
		
		# End manipulate record
		
		
		if should_update:
			table.update_item(updated_record)
			ui.body("  Updated")
			updated_cnt += 1
	
	return True, "  Updates: " + ui.format_text(updated_cnt, 'bold')

def new_artist(args, opts):
	collective = moses_common.collective.Collective(log_level=log_level, dry_run=dry_run)
	artist_table = moses_common.dynamodb.Table('artintelligence.gallery-collective', log_level=log_level, dry_run=dry_run)
	
	if 'target' not in args:
		return False, "Artist name required"
	
	artist_name = args['target']
	artist_id = common.convert_to_snakecase(common.normalize(artist_name, strip_single_chars=False))
	ui.header(artist_id)
	names = artist_name.split(r' ')
	sort_name = names.pop()
	if len(names):
		sort_name += ', ' + ' '.join(names)
	
	dt = common.get_dt_now()
	new_artist_record = {
		"id": artist_id,
		"bio": "",
		"born": "",
		"country": "",
		"create_time": common.convert_datetime_to_string(dt),
		"died": "",
		"external_url": "",
		"name": artist_name,
		"sort_name": sort_name,
		"update_time": common.convert_datetime_to_string(dt)
	}
	artist = moses_common.collective.Artist(collective, new_artist_record, log_level=log_level, dry_run=dry_run)
	
	new_work_record = {
		"artist_id": artist_id,
		"name": "default",
		"create_time": common.convert_datetime_to_string(dt),
		"update_time": common.convert_datetime_to_string(dt),
		"aspect_ratios": [],
		"locations": [],
		"methods": [],
		"modifiers": [],
		"style": [],
		"subjects": [],
		"time_period": None
	}
	genre = moses_common.collective.Genre(artist, new_work_record, log_level=log_level, dry_run=dry_run)

	artist_table.put_item(new_artist_record)
	collective.set_artists_update()
	
	genre.save()
	return True, "Added artist and work"


def update_collective_records(opts):
	table = moses_common.dynamodb.Table('artintelligence.gallery-collective', log_level=log_level, dry_run=dry_run)
	artists = table.scan()
	
	cnt = 0
	updated_cnt = 0
	total = len(artists)
	for artist in artists:
		if 'wikipedia_url' not in artist:
			print(f"Skipping {artist['id']}")
			continue
		if opts['limit'] and common.convert_to_int(opts['limit']) <= cnt:
			break
		cnt += 1
		should_update = False
		ui.header(artist['id'])
		
		
		
		
		# Start manipulate record
		dt = common.get_dt_now()
		
		new_record = {
			"update_time": common.convert_datetime_to_string(dt),
			
			"id": artist['id']
		}
# 		ui.pretty(new_record)
# 		print("artist.categories {}: {}".format(type(artist.categories), artist.categories))
		
		# End manipulate record
		
		
		
		
		table.update_item(new_record, ['wikipedia_summary', 'wikipedia_title', 'wikipedia_url'])
		ui.body("  Updated")
		updated_cnt += 1
	
	if updated_cnt:
		collective.set_artists_update()
	return True, "  Updates: " + ui.format_text(updated_cnt, 'bold')


def update_work_records(opts):
	collective = moses_common.collective.Collective(log_level=log_level, dry_run=dry_run)
	
	table = moses_common.dynamodb.Table('artintelligence.gallery-works', log_level=log_level, dry_run=dry_run)
	works = table.scan()
	
	cnt = 0
	inserted_cnt = 0
	updated_cnt = 0
	deleted_cnt = 0
	total = len(works)
	for work in works:
		if opts['limit'] and common.convert_to_int(opts['limit']) <= cnt:
			break
		should_insert = False
		should_update = False
		should_delete = False
		dt = common.get_dt_now()
		insert_record = work.copy()
		insert_record['create_time'] = common.convert_datetime_to_string(dt)
		insert_record['update_time'] = common.convert_datetime_to_string(dt)
		update_record = {
			"update_time": common.convert_datetime_to_string(dt),
			"artist_id": work['artist_id'],
			"name": work['name']
		}
		delete_record = {
			"artist_id": work['artist_id'],
			"name": work['name']
		}
		
		
		
		# Filter
		if work['name'] in ['expressionist', 'abstract']:
			continue
		json = common.make_json(work['styles'])
		if not re.search(r'cubism', json, re.IGNORECASE):
			continue
		
		cnt += 1
		ui.header(work['artist_id'] + ' - ' + work['name'])
# 		print("work['methods'] {}: {}".format(type(work['methods']), work['methods']))
		print("json {}: {}".format(type(json), json))
		
		# Start manipulate record
		artist = collective.get_artist_by_id(work['artist_id'])
		method_map = {
			"acrylic painting": "paintings",
			"aquatint print": "prints",
			"chalk drawing": "drawings",
			"charcoal drawing": "drawings",
			"collage": "collages",
			"concept art": "art",
			"digital picture": "pictures",
			"engraving": "engravings",
			"fresco": "frescos",
			"frieze": "friezes",
			"glass sculpture": "sculptures",
			"gouache painting": "paintings",
			"illustration": "illustrations",
			"linocut": "linocuts",
			"lithograph": "lithographs",
			"marble sculpture": "sculptures",
			"oil painting": "paintings",
			"painting": "paintings",
			"pastel": "paintings",
			"pen and ink drawing": "drawings",
			"pencil drawing": "drawings",
			"pencil sketch": "sketches",
			"photograph": "photographs",
			"print illustration": "illustrations",
			"screen print": "prints",
			"sculpture": "sculptures",
			"tempera painting": "paintings",
			"watercolor painting": "paintings",
			"woodblock print": "prints",
			"woodcut": "woodcuts"
		}
		
		# For updating subjects to GPT
		methods = []
		for method in work['methods']:
			method = re.sub(r'^\d:', '', method)
			if method not in method_map:
				ui.warning(f"{method} not in list")
				continue
			methods.append(method_map[method])
		method_string = ' and '.join(methods)
		update_record['subjects'] = [
			f"!Generate a list of 20 descriptions of content of {method_string} by {artist.name}."
		]
		print("update_record['subjects'] {}: {}".format(type(update_record['subjects']), update_record['subjects']))
		should_update = True
		
# 		styles = []
# 		for style in work['styles']:
# 			if re.match(r'\b(author|Blizzard|Marvel|DC Comics|game art|Hearthstone|Watchmen)\b', style, re.IGNORECASE):
# 				continue
# 			styles.append(style)
# 		
# 		update_record['styles'] = ['1:' + ', '.join(styles)]
# 		print("update_record['styles'] {}: {}".format(type(update_record['styles']), update_record['styles']))
# 		should_update = True
		if dry_run:
			continue
		
# 		update_record['methods'] = []
# 		for method in work['methods']:
# 			if method == '1:ink drawing':
# 				update_record['methods'].append('1:pen and ink drawing')
# 				should_update = True
# 			else:
# 				update_record['methods'].append(method)
				
# 		if work['name'] == 'default':
# 			do_insert = True
# 			for work2 in works:
# 				if work2['artist_id'] == work['artist_id'] and work2['name'] != 'default':
# 					do_insert = False
# 			if do_insert:
# 				inserted_cnt['name'] = 'comic book'
# 				inserted_cnt['locations'] = []
# 				should_insert = True
# 			
# 			table.delete_item(update_record['artist_id'], update_record['name'])
# 			should_delete = True
		
		# End manipulate record
		
		
		
		
		if should_insert:
			ui.pretty(insert_record)
			table.put_item(insert_record)
			ui.body("  Inserted")
			inserted_cnt += 1
			time.sleep(1)
		if should_update:
			ui.pretty(update_record)
			table.update_item(update_record)
			ui.body("  Updated")
			updated_cnt += 1
			time.sleep(1)
		if should_delete:
			ui.pretty(delete_record)
			table.delete_item(delete_record['artist_id'], delete_record['name'])
			ui.body("  Updated")
			updated_cnt += 1
			time.sleep(1)
	
	if inserted_cnt + updated_cnt + deleted_cnt:
		collective.set_genres_update()
	return True, "  Inserts: " + ui.format_text(inserted_cnt, 'bold') + "\n  Updates: " + ui.format_text(updated_cnt, 'bold') + "\n  Deletes: " + ui.format_text(deleted_cnt, 'bold')


def update_image_records(opts):
	collective = moses_common.collective.Collective(log_level=log_level, dry_run=dry_run)
	
	table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
	records = table.scan()
	
	cnt = 0
	inserted_cnt = 0
	updated_cnt = 0
	deleted_cnt = 0
	total = len(records)
	for record in records:
		if opts['limit'] and common.convert_to_int(opts['limit']) <= cnt:
			break
		should_insert = False
		should_update = False
		should_delete = False
		dt = common.get_dt_now()
		insert_record = record.copy()
		insert_record['create_time'] = common.convert_datetime_to_string(dt)
		insert_record['update_time'] = common.convert_datetime_to_string(dt)
		update_record = {
			"update_time": common.convert_datetime_to_string(dt),
			"filename": record['filename'],
			"create_time": record['create_time']
		}
		delete_record = {
			"filename": record['filename'],
			"create_time": record['create_time']
		}
		
		
		# Filter
		if 'aspect_ratio' in record:
			continue
		cnt += 1
		ui.header(record['filename'])
		
		
		
		
		# Start manipulate record
		update_record['aspect_ratio'] = common.round_half_up(common.convert_to_int(record['width']) / common.convert_to_int(record['height']), 2)
		should_update = True
		
		# End manipulate record
		
		
		if should_insert:
			ui.pretty(insert_record)
			table.put_item(insert_record)
			ui.body("  Inserted")
			inserted_cnt += 1
			time.sleep(1)
		if should_update:
			ui.pretty(update_record)
			table.update_item(update_record)
			ui.body("  Updated")
			updated_cnt += 1
			time.sleep(1)
		if should_delete:
			ui.pretty(delete_record)
			table.delete_item(delete_record['artist_id'], delete_record['name'])
			ui.body("  Updated")
			updated_cnt += 1
			time.sleep(1)
	
	if inserted_cnt + updated_cnt + deleted_cnt:
		collective.set_images_update()
	return True, "  Inserts: " + ui.format_text(inserted_cnt, 'bold') + "\n  Updates: " + ui.format_text(updated_cnt, 'bold') + "\n  Deletes: " + ui.format_text(deleted_cnt, 'bold')

def search(args, opts):
	if args.get('target') not in ['artists', 'works', 'images']:
		return False, "Target must be one of 'artists', 'works', or 'images'"
	
	target = args.get('target')
	if target == 'artists':
		target = 'collective'
	table = moses_common.dynamodb.Table(f"artintelligence.gallery-{target}", log_level=log_level, dry_run=dry_run)
	records = table.scan()
	
	if not args['args'] or not args['args'][0]:
		return False, "Search expression required"
	search_string = args['args'][0]
	rematch = re.compile(search_string, re.IGNORECASE)
	resub = re.compile(r' *(' + search_string + ') *', re.IGNORECASE)
	
	cnt = 0
	total = len(records)
	for record in records:
		if opts['limit'] and common.convert_to_int(opts['limit']) <= cnt:
			break
		
		# Filter
		json = common.make_json(record)
		if not rematch.search(json):
			continue
		
		header = record[table.partition_key.name]
		if table.sort_key:
			header += ' - ' + record[table.sort_key.name]
		ui.header(header)
		
		for key, value in record.items():
			if type(value) is list:
				json = common.make_json(value)
				if rematch.search(json):
					json = resub.sub(r' `\1` ', json)
					ui.body(f"  *{key}:* {json}")
			elif type(value) is str:
				if rematch.search(value):
					value = resub.sub(r' `\1` ', value)
					ui.body(f"  *{key}:* {value}")
		
		cnt += 1
		
	return True, "  Found: " + ui.format_text(cnt, 'bold')


def test(opts):
	collective = moses_common.collective.Collective(openai_api_key='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', log_level=log_level, dry_run=dry_run)
	artist = collective.get_artist_by_id('anato_finnstark')
	genres = artist.genres
	genre = genres[0]
	subject = genre.choose_subject()
	print("subject {}: {}".format(type(subject), subject))
	return True, subject
# 	gpt = moses_common.openai.GPT(openai_api_key='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', log_level=log_level, dry_run=dry_run)
# # 	results = gpt.chat('Generate a list of 20 subjects of illustrations by Anato Finnstark.', strip_double_quotes=True)
# # 	print("results {}".format(type(results)))
# # 	print(results)
# 	tags = gpt.process_list(results)
# 	print("tags {}: {}".format(type(tags), tags))
# 	return True, ''


def update_url(args, opts):
	collective = moses_common.collective.Collective(log_level=log_level, dry_run=dry_run)
	artist = collective.get_artist_by_name(args['target'])
	if not artist:
		return False, f"Artist '{artist_name}' not found"
	
	ui.title(artist.name)
	should_update = False
	if args['args'] and re.match(r'https://en\.wikipedia\.org/wiki/', args['args'][0]):
		artist.external_url = args['args'][0]
		should_update = True
	
	if not artist.external_url:
		return False, f"No artist URL"
	if re.match(r'https://en\.wikipedia\.org/wiki/', artist.external_url):
		name = re.sub(r'https://en\.wikipedia\.org/wiki/', '', artist.external_url)
		page = get_wikipedia_page(name)
		if page:
			ui.body("  Found " + page.title)
			artist.bio = page.summary
			should_update = True
	
	
	if not should_update:
		return False, "Nothing to update"
	
	success = artist.save()
	if not success:
		return False, "Failed to update artist"
	return True, "Updated artist"





"""
weight, tag = split_tag(category)
"""
def split_tag(cat):
	weight = 1
	tag = cat
	if re.search(r':', cat):
		parts = cat.split(':')
		if not parts[1]:
			parts[1] = None
		weight = common.convert_to_int(parts[0])
		tag = parts[1]
	return weight, tag


def get_art_forms():
	art_forms = {
		"illustration": {
			"name": "illustration",
			"methods": ["aquatint", "chalk", "charcoal", "engraving", "ink", "linocut", "lithography", "pencil", "print", "screen print", "watercolor", "woodcut", "woodblock print"]
		},
		"drawing": {
			"name": "drawing"
		},
		"painting": {
			"name": "painting",
			"methods": ["acrylic", "gouache", "guache", "lithography", "oil", "pastel", "pastels", "tempera", "watercolor"]
		},
		"photography": {
			"name": "photograph",
			"methods": []
		},
		"sculpture": {
			"name": "sculpture",
			"methods": ["marble"]
		}
	}
	art_forms['drawing']['methods'] = art_forms['illustration']['methods']
	return art_forms

def get_full_model_name(short_name=None):
	if short_name == 'sdxl09':
		return "Stable Diffusion XL 0.9"
	elif short_name == 'sdxl10':
		return "Stable Diffusion XL 1.0"
	elif short_name in ['sdxl', 'sd']:
		return "Stable Diffusion XL Beta"
	elif short_name == 'sd15':
		return "Stable Diffusion 1.5"
	elif short_name == 'del':
		return "Deliberate V2"
	elif short_name == 'ds':
		return "DreamShaper"
	elif short_name == 'rv':
		return "Realistic Vision"
	else:
		return None


def get_wikipedia_page(artist_name):
	try:
		page = wikipedia.page(artist_name, auto_suggest=False)
	except:
		page = None
	if not page:
		try:
			titles = wikipedia.search(artist_name)
			page = wikipedia.page(titles[0], auto_suggest=False)
		except:
			page = None
	return page


def get_collective():
	artist_list_location = '/opt'
	if common.is_local():
		artist_list_location=os.environ['HOME']
	return visual_artists.Collective(artist_list_location, log_level=log_level, dry_run=dry_run)

def get_png_info(data):
	png_info = PngInfo()
	flat_data = common.flatten_hash(data)
	for key, value in flat_data.items():
		png_info.add_text(key, str(value))
	return png_info


if __name__ == '__main__':
	args, opts = ui.get_options({
		"args": [ {
			"name": "action",
			"label": "Action",
			"required": True
		}, {
			"name": "target",
			"label": "Target"
# 		}, {
# 			"name": "file",
# 			"label": "File",
# 			"type": "glob"
		}],
		"options": [ {
			"short": "v",
			"long": "verbose"
		}, {
			"short": "x",
			"long": "extra_verbose"
		}, {
			"short": "n",
			"long": "dry_run"
		}, {
			"short": "l",
			"long": "limit",
			"type": "input"
		},
		
		{
			"short": "d",
			"long": "delete"
		} ]
	})
	if opts['limit']:
		opts['limit'] = common.convert_to_int(opts['limit']);
	dry_run, log_level, limit = common.set_basic_args(opts)

	handler(args, opts)

