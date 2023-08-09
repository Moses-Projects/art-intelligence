#!/usr/bin/env python

import datetime
import os
import re
import subprocess
import sys

from PIL import Image
from PIL.PngImagePlugin import PngInfo

import moses_common.__init__ as common
import moses_common.dynamodb
import moses_common.s3
import moses_common.stabilityai
import moses_common.ui
import moses_common.visual_artists as visual_artists


ui = moses_common.ui.Interface(use_slack_format=True, usage_message="""
Manage images and data for Art Intelligence.
  manage.py astats             # Display category stats on artists db
  manage.py deploy             # Copy dev S3 website to prod S3
  manage.py fail               # Send local fail website to S3
  manage.py import <filename>  # Import images from image_generator.py
  manage.py refresh            # Grab new artist file and sync to db
  manage.py send               # Send local dev website to S3
  manage.py stats              # Display category stats on image db
  manage.py artist_db          # Update artist db - requires editing
  manage.py image_db           # Update image db - requires editing
  
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
		subprocess.run(f"aws s3 sync{dry_run_arg} --delete --exclude manage.html s3://artintelligence.gallery/dev s3://artintelligence.gallery/web", shell=True)
	elif action == 'engines':
		success, response = get_engine_list()
	elif action == 'fail':
		os.chdir("/Users/tim/Repositories/art-intelligence")
		subprocess.run(f"aws s3 sync{dry_run_arg} --delete fail s3://artintelligence.gallery/fail", shell=True)
	elif action == 'import':
		success, response = import_image(args['file'], opts)
	elif action == 'refresh':
		success, response = refresh_artists(opts)
	elif action == 'send':
		os.chdir("/Users/tim/Repositories/art-intelligence")
		subprocess.run(f"aws s3 sync{dry_run_arg} --delete dev s3://artintelligence.gallery/dev", shell=True)
	elif action == 'stats':
		success, response = get_image_stats(opts)
	elif action == 'artist_db':
		success, response = update_artist_records(opts)
	elif action == 'image_db':
		success, response = update_image_records(opts)
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
	should_change = False
	if 'filename' not in record or not record['filename'] or record['filename'] == "None":
		should_change = True
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
	required = ['filename', 'create_time', 'engine_label', 'engine_name', 'prompt', 'seed', 'steps', 'cfg_scale', 'width', 'height', 'negative_prompt', 'query-artist', 'query-model', 'query', 'orientation', 'aspect']
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
			should_change = True
			ui.body(f"  query-model is '{record['query-model']}' but should be '{artist.checkpoint}'")
			record['query-model'] = artist.checkpoint
	
	
	# Update file
	if should_change:
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
	
	if re.match(r'sd', part[1]):
		record["engine_label"] = get_full_model_name(part[1])
		record["engine_name"] = part[1]
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
	table = moses_common.dynamodb.Table('artintelligence.gallery-artists')
	records = table.scan()
	
	cnt = 0
	updated_cnt = 0
	total = len(records)
	for record in records:
		if opts['limit'] and opts['limit'] <= cnt:
			break
		cnt += 1
		should_change = False
		ui.header(record['id'])
		updated_record = {
			"id": record['id']
		}
		
		
		# Start manipulate record
		
		if 'preferred_model' not in record:
			updated_record['preferred_model'] = 'sdxl'
			should_change = True
		
		# End manipulate record
		
		
		if should_change:
			table.update_item(updated_record)
			ui.body("  Updated")
			updated_cnt += 1
	
	return True, "  Updates: " + ui.format_text(updated_cnt, 'bold')


def update_image_records(opts):
# 	collective = get_collective()
	table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
	records = table.scan()
	
	cnt = 0
	updated_cnt = 0
	total = len(records)
	for record in records:
		updated_record = {
			"filename": record['filename'],
			"create_time": record['create_time']
		}
		if opts['limit'] and opts['limit'] <= cnt:
			break
		cnt += 1
		should_change = False
		ui.header(record['filename'])
		
		
		# Start manipulate record
		
# 		artist = collective.get_artist(record['query-artist'])
		
		if record.get('query-model') in ['sdxl', 'sd15', 'del', 'ds', 'rv']:
			updated_record['query-model'] = get_full_model_name(record['query-model'])
			should_change = True
		
		# End manipulate record
		
		
		if should_change:
			table.update_item(updated_record)
			ui.body("  Updated")
			updated_cnt += 1
	
	return True, "  Updates: " + ui.format_text(updated_cnt, 'bold')


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
			"name": "file",
			"label": "File",
			"type": "glob"
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
	dry_run, log_level, limit = common.set_basic_args(opts)

	handler(args, opts)

