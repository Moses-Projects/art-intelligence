#!/usr/bin/env python

import json
import os
import re
import requests
import sys


sys.path.append('/opt')

import moses_common.__init__ as common
import moses_common.api_gateway
import moses_common.dynamodb
import moses_common.s3
import moses_common.secrets_manager
import moses_common.sinkinai
import moses_common.stabilityai
import moses_common.ui
import moses_common.visual_artists as visual_artists


ui = moses_common.ui.Interface(use_slack_format=True, usage_message="""
Generate and store images in S3 and DynamoDB.
  generate.py
  
  Options:
    -h, --help                  This help screen.
    -n, --dry_run               Run without updating anything.
    -v, --verbose               More output.
    -x, --extra_verbose         Even more output.
""")


log_level = 5
if common.get_environment() == 'dev':
	log_level = 6
limit = None
dry_run = False

secret = moses_common.secrets_manager.Secret('artintelligence.gallery/api_keys')
api_keys = secret.get_value()

artist_list_location = '/tmp'
if common.is_local():
	artist_list_location=os.environ['HOME']
collective = visual_artists.Collective(artist_list_location=artist_list_location, log_level=log_level, dry_run=dry_run)


def handler(event, context):
	api = moses_common.api_gateway.Request(event, log_level=log_level, dry_run=dry_run)
	
	path = api.parse_path()
	
	method = api.method
	
	query, metadata = api.process_query()
	body = api.body
	
	output = {}
	if len(path) >= 1:
		action = path.pop(0)
		if action == 'generate':
			if method == 'POST':
				output = generate(event)
				if type(output) is str:
					output = { "status": 503, "error": output }
			else:
				output = { "status": 405, "error": "Method not allowed" }
		else:
			return { "statusCode": 500, "body": "Invalid API" }
	else:
		return { "statusCode": 500, "body": "Invalid API" }
	
	
	if type(output) is dict and 'status' in output and 'error' in output:
		print("ERROR: {} - {}".format(output['status'], output['error']))
		return {
			"statusCode": output['status'],
			"body": output['error']
		}
	
	if type(output) is list:
		output = {
			'count': len(output),
			'results': output
		}
	response = {
		"statusCode": 200,
		"body": common.make_json(output)
	}
	
#	print("response {}: {}".format(type(response), response))
	return response



def generate(event):
	# Get artist
	artist = collective.get_artist(event.get('artist'))
	if not artist:
		# Get subject
		subject = collective.choose_subject()
		print("subject {}: {}".format(type(subject), subject))
		
		artist = collective.choose_artist(subject)
	print(f"{artist.name}: {artist.categories}")
	
	# Get prompt
	prompt = get_prompt(artist)
	
	# Get image config
	data = get_image(prompt)
	if type(data) is str:
		return data
	
	# Send to Art Intelligence bucket and db
	data['query']['model'] = artist.full_model
	success = send_image(data)
	
	return data



def get_prompt(artist, subject=None):
	query = artist.get_query(subject)
	
	prompt = visual_artists.Prompt(query, log_level=log_level, dry_run=dry_run)
	prompt.generate(api_keys['OPENAI_API_KEY'])
	return prompt


def get_image(prompt):
	neg_prompt = prompt.get_negative_prompt('sinkin')
	
	filename_prefix = None
	if prompt.data and 'query' in prompt.data and 'artist' in prompt.data['query']:
		filename_prefix = common.convert_to_snakecase(common.normalize(prompt.data['query']['artist']))
	
	# Set save directory
	save_directory = '/tmp'
	if common.is_local():
		save_directory = os.environ.get('HOME') + '/Downloads'
	
	success = False
	data = "Model not recognized"
	if re.match(r'sd', prompt.model):
		# Set up stable diffusion
		stable_diffusion = moses_common.stabilityai.StableDiffusion(
			stability_key = api_keys['STABILITY_API_KEY'],
			model = prompt.model,
			save_directory = save_directory,
			log_level = log_level,
			dry_run = dry_run
		)
		
		# Get data for image
		data = stable_diffusion.text_to_image(
			prompt.prompt,
			negative_prompt=neg_prompt,
# 			seed=opts['seed'],
# 			steps=opts['steps'],
# 			cfg_scale=opts['cfg'],
# 			width=prompt.data['width'],
# 			height=prompt.data['height'],
			filename_prefix=filename_prefix,
			return_args=True,
			orientation=prompt.data.get('orientation'),
			aspect=prompt.data.get('aspect')
		)
		if prompt.data and 'query' in prompt.data:
			data['query'] = prompt.data['query']
		
		# Generate image
		success, data = stable_diffusion.text_to_image(data)
	
	else:
		# Set up sinkai
		sinkinai = moses_common.sinkinai.SinkinAI(
			sinkinai_api_key = api_keys['SINKIN_API_KEY'],
			save_directory = save_directory,
			log_level = log_level,
			dry_run = dry_run
		)
		
		# Get data for image
		data = sinkinai.text_to_image(
			prompt.prompt,
			model=prompt.model,
			negative_prompt=neg_prompt,
# 			seed=opts['seed'],
# 			steps=opts['steps'],
# 			cfg_scale=opts['cfg'],
# 			width=prompt.data['width'],
# 			height=prompt.data['height'],
			filename_prefix=filename_prefix,
			return_args=True,
			orientation=prompt.data.get('orientation'),
			aspect=prompt.data.get('aspect')
		)
		
		# Add further data
		if prompt.data and 'query' in prompt.data:
			data['query'] = prompt.data['query']
		
		# Generate image
		success, data = sinkinai.text_to_image(data)
	
	if not success:
		return data
	return data


def send_image(data):
	bucket_name = 'artintelligence.gallery'
	object_name = f"images/{data['filename']}"
	
# 	print("data['filepath'] {}: {}".format(type(data['filepath']), data['filepath']))
	
	bucket = moses_common.s3.Bucket(bucket_name, log_level=log_level, dry_run=dry_run)
	file = moses_common.s3.Object(bucket, object_name, log_level=log_level, dry_run=dry_run)
	response = file.upload_file(data['filepath'])
	
	del(data['filepath'])
	data['image_url'] = f"https://{bucket_name}/{object_name}"
# 	print("data['image_url'] {}: {}".format(type(data['image_url']), data['image_url']))
	flat_data = common.flatten_hash(data)
	table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
	table.put_item(flat_data)
	return True


if __name__ == '__main__':
	args, opts = ui.get_options({
		"args": [ {
			"name": "arg",
			"label": "Arg"
		} ],
		"options": [ {
			"short": "a",
			"long": "artist",
			"type": "input"
		}, {
			"short": "l",
			"long": "limit",
			"type": "input"
		}, {
			"short": "n",
			"long": "dry_run"
		}, {
			"short": "v",
			"long": "verbose"
		}, {
			"short": "x",
			"long": "extra_verbose"
		} ]
	})
	dry_run, log_level, limit = common.set_basic_args(opts)
	collective.log_level = log_level
	
	args['artist'] = opts['artist']
	
	args["method"] = "POST"
	args["path"] = "/generate"
	
	response = handler(args, {})
	ui.pretty(response)
	data = common.parse_json(response['body'])
	print("data {}: {}".format(type(data), data))
	save_directory=os.environ['HOME'] + '/Downloads'
	os.system(f"open {save_directory}/{data['filename']}")

