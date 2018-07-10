from sacred import Experiment
import tensorflow as tf
from tensorflow.contrib import tpu
import numpy as np
import os

import Datasets
from Input import Input as Input
from Input import batchgenerators as batchgen
from Input import musdb_input
import Utils
import Models.UnetSpectrogramSeparator
import Models.UnetAudioSeparator
import cPickle as pickle
import Test
import Evaluate

import functools
from tensorflow.contrib.signal.python.ops import window_ops
from tensorflow.contrib.cluster_resolver import TPUClusterResolver

ex = Experiment('Waveunet')

@ex.config
def cfg():
    # Base configuration
    model_config = {"musdb_path": "gs://vimsstfrecords/musdb18", # SET MUSDB PATH HERE, AND SET CCMIXTER PATH IN
                    # CCMixter.xml
                    "estimates_path": "gs://vimsscheckpoints", # SET THIS PATH TO WHERE YOU WANT SOURCE ESTIMATES
                    # PRODUCED BY THE TRAINED MODEL TO BE SAVED. Folder itself must exist!
                    "model_base_dir": "checkpoints", # Base folder for model checkpoints
                    "log_dir": "logs", # Base folder for logs files
                    "batch_size": 16, # Batch size
                    "init_sup_sep_lr": 1e-4, # Supervised separator learning rate
                    "epoch_it" : 2000, # Number of supervised separator steps per epoch
                    "num_disc": 5,  # Number of discriminator iterations per separator update
                    'cache_size' : 16, # Number of audio excerpts that are cached to build batches from
                    'num_workers' : 6, # Number of processes reading audio and filling up the cache
                    "duration" : 2, # Duration in seconds of the audio excerpts in the cache. Has to be at least the output length of the network!
                    'min_replacement_rate' : 16,  # roughly: how many cache entries to replace at least per batch on average. Can be fractional
                    'num_layers' : 12, # How many U-Net layers
                    'filter_size' : 15, # For Wave-U-Net: Filter size of conv in downsampling block
                    'merge_filter_size' : 5, # For Wave-U-Net: Filter size of conv in upsampling block
                    'num_initial_filters' : 24, # Number of filters for convolution in first layer of network
                    "num_frames": 16384, # DESIRED number of time frames in the output waveform per samples (could be changed when using valid padding)
                    'expected_sr': 22050,  # Downsample all audio input to this sampling rate
                    'mono_downmix': True,  # Whether to downsample the audio input
                    'output_type' : 'direct', # Type of output layer, either "direct" or "difference". Direct output: Each source is result of tanh activation and independent. DIfference: Last source output is equal to mixture input - sum(all other sources)
                    'context' : False, # Type of padding for convolutions in separator. If False, feature maps double or half in dimensions after each convolution, and convolutions are padded with zeros ("same" padding). If True, convolution is only performed on the available mixture input, thus the output is smaller than the input
                    'network' : 'unet', # Type of network architecture, either unet (our model) or unet_spectrogram (Jansson et al 2017 model)
                    'upsampling' : 'linear', # Type of technique used for upsampling the feature maps in a unet architecture, either 'linear' interpolation or 'learned' filling in of extra samples
                    'task' : 'voice', # Type of separation task. 'voice' : Separate music into voice and accompaniment. 'multi_instrument': Separate music into guitar, bass, vocals, drums and other (Sisec)
                    'augmentation' : True, # Random attenuation of source signals to improve generalisation performance (data augmentation)
                    'raw_audio_loss' : True # Only active for unet_spectrogram network. True: L2 loss on audio. False: L1 loss on spectrogram magnitudes for training and validation and test loss
                    }
    seed=1337
    experiment_id = np.random.randint(0,1000000)

    model_config["num_sources"] = 4 if model_config["task"] == "multi_instrument" else 2
    model_config["num_channels"] = 1 if model_config["mono_downmix"] else 2

@ex.named_config
def baseline():
    print("Training baseline model")

@ex.named_config
def baseline_diff():
    print("Training baseline model with difference output")
    model_config = {
        "output_type" : "difference"
    }

@ex.named_config
def baseline_context():
    print("Training baseline model with difference output and input context (valid convolutions)")
    model_config = {
        "output_type" : "difference",
        "context" : True
    }

@ex.named_config
def baseline_stereo():
    print("Training baseline model with difference output and input context (valid convolutions)")
    model_config = {
        "output_type" : "difference",
        "context" : True,
        "mono_downmix" : False
    }

@ex.named_config
def full():
    print("Training full singing voice separation model, with difference output and input context (valid convolutions) and stereo input/output, and learned upsampling layer")
    model_config = {
        "output_type" : "difference",
        "context" : True,
        "upsampling": "learned",
        "mono_downmix" : False
    }

@ex.named_config
def baseline_context_smallfilter_deep():
    model_config = {
        "output_type": "difference",
        "context": True,
        "num_layers" : 14,
        "duration" : 7,
        "filter_size" : 5,
        "merge_filter_size" : 1
    }

@ex.named_config
def full_multi_instrument():
    print("Training multi-instrument separation with best model")
    model_config = {
        "output_type": "difference",
        "context": True,
        "upsampling": "linear",
        "mono_downmix": False,
        "task" : "multi_instrument"
    }

@ex.named_config
def baseline_comparison():
    model_config = {
        "batch_size": 4, # Less output since model is so big.
        # Doesn't matter since the model's output is not dependent on its output or input size (only convolutions)
        "cache_size": 4,
        "min_replacement_rate" : 4,

        "output_type": "difference",
        "context": True,
        "num_frames" : 768*127 + 1024,
        "duration" : 13,
        "expected_sr" : 8192,
        "num_initial_filters" : 34
    }

@ex.named_config
def unet_spectrogram():
    model_config = {
        "batch_size": 4, # Less output since model is so big.
        "cache_size": 4,
        "min_replacement_rate" : 4,

        "network" : "unet_spectrogram",
        "num_layers" : 6,
        "expected_sr" : 8192,
        "num_frames" : 768 * 127 + 1024, # hop_size * (time_frames_of_spectrogram_input - 1) + fft_length
        "duration" : 13,
        "num_initial_filters" : 16
    }

@ex.named_config
def unet_spectrogram_l1():
    model_config = {
        "batch_size": 4, # Less output since model is so big.
        "cache_size": 4,
        "min_replacement_rate" : 4,

        "network" : "unet_spectrogram",
        "num_layers" : 6,
        "expected_sr" : 8192,
        "num_frames" : 768 * 127 + 1024, # hop_size * (time_frames_of_spectrogram_input - 1) + fft_length
        "duration" : 13,
        "num_initial_filters" : 16,
        "loss" : "magnitudes"
    }


@ex.capture
def train(model_config, experiment_id, sup_dataset, unsup_dataset=None, load_model=None):
    # Determine input and output shapes
    disc_input_shape = [model_config["batch_size"], model_config["num_frames"], 0]  # Shape of input
    if model_config["network"] == "unet":
        separator_class = Models.UnetAudioSeparator.UnetAudioSeparator(model_config["num_layers"], model_config["num_initial_filters"],
                                                                   output_type=model_config["output_type"],
                                                                   context=model_config["context"],
                                                                   mono=model_config["mono_downmix"],
                                                                   upsampling=model_config["upsampling"],
                                                                   num_sources=model_config["num_sources"],
                                                                   filter_size=model_config["filter_size"],
                                                                   merge_filter_size=model_config["merge_filter_size"])
    elif model_config["network"] == "unet_spectrogram":
        separator_class = Models.UnetSpectrogramSeparator.UnetSpectrogramSeparator(model_config["num_layers"], model_config["num_initial_filters"],
                                                                       mono=model_config["mono_downmix"],
                                                                       num_sources=model_config["num_sources"])
    else:
        raise NotImplementedError

    sep_input_shape, sep_output_shape = separator_class.get_padding(np.array(disc_input_shape))
    separator_func = separator_class.get_output

    # Creating the batch generators
    # TODO rewrite this part to use pre-processed tf.records (with fixed input size)

    # Placeholders and input normalisation
    # mix_context, sources = Input.get_multitrack_placeholders(sep_output_shape, model_config["num_sources"],
    # sep_input_shape, "sup")
    # mix = Utils.crop(mix_context, sep_output_shape)

    print("Training...")

    # BUILD MODELS
    # Separator
    # input: Input batch of mixtures, 3D tensor [batch_size, num_samples, num_channels]
    # Sources are output in order [acc, voice] for voice separation, [bass, drums, other, vocals] for multi-instrument separation
    separator_sources = separator_func(mix_context, True, not model_config["raw_audio_loss"], reuse=False)
    # Supervised objective: MSE in log-normalized magnitude space
    separator_loss = 0
    for (real_source, sep_source) in zip(sources, separator_sources):
        #if model_config["network"] == "unet_spectrogram" and not model_config["raw_audio_loss"]:
        #    window = functools.partial(window_ops.hann_window, periodic=True)
        #    stfts = tf.contrib.signal.stft(tf.squeeze(real_source, 2), frame_length=1024, frame_step=768,
        #                                   fft_length=1024, window_fn=window)
        #    real_mag = tf.abs(stfts)
        #    separator_loss += tf.reduce_mean(tf.abs(real_mag - sep_source))
        #else:
        separator_loss += tf.reduce_mean(tf.square(real_source - sep_source))
    separator_loss = separator_loss / float(len(sources)) # Normalise by number of sources

    # TRAINING CONTROL VARIABLES
    global_step = tf.get_variable('global_step', [], initializer=tf.constant_initializer(0), trainable=False, dtype=tf.int64)
    increment_global_step = tf.assign(global_step, global_step + 1)
    sep_lr = tf.get_variable('unsup_sep_lr', [],initializer=tf.constant_initializer(model_config["init_sup_sep_lr"], dtype=tf.float32), trainable=False)

    # Set up optimizers
    separator_vars = Utils.getTrainableVariables("separator")
    print("Sep_Vars: " + str(Utils.getNumParams(separator_vars)))
    print("Num of variables" + str(len(tf.global_variables())))

    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        with tf.variable_scope("separator_solver"):
            separator_solver = tf.train.AdamOptimizer(learning_rate=sep_lr).minimize(separator_loss, var_list=separator_vars)
            separator_solver = tf.contrib.tpu.CrossShardOptimizer(separator_solver)

    # SUMMARAIES
    tf.summary.scalar("sep_loss", separator_loss, collections=["sup"])
    sup_summaries = tf.summary.merge_all(key='sup')

    # Start session and queue input threads
    config = tf.ConfigProto()

    tpu_grpc_url = TPUClusterResolver(
        tpu=[os.environ['TPU_NAME']]).get_master()

    config.gpu_options.allow_growth = True
    sess = tf.Session(target=tpu_grpc_url, config=config)
    sess.run(tpu.initialize_system())
    sess.run(tf.global_variables_initializer())

    writer = tf.summary.FileWriter(model_config["log_dir"] + os.path.sep + str(experiment_id), graph=sess.graph)

    # CHECKPOINTING
    # Load pretrained model to continue training, if we are supposed to
    if load_model != None:
        restorer = tf.train.Saver(tf.global_variables(), write_version=tf.train.SaverDef.V2)
        print("Num of variables" + str(len(tf.global_variables())))
        restorer.restore(sess, load_model)
        print('Pre-trained model restored from file ' + load_model)

    saver = tf.train.Saver(tf.global_variables(), write_version=tf.train.SaverDef.V2)

    # Start training loop
    run = True
    _global_step = sess.run(global_step)
    _init_step = _global_step
    it = 0
    while run:
        # TRAIN SEPARATOR
        sup_batch = sup_batch_gen.get_batch()
        feed = {i:d for i,d in zip(sources, sup_batch[1:])}
        feed.update({mix_context : sup_batch[0]})
        _, _sup_summaries = sess.run([separator_solver, sup_summaries], feed)
        writer.add_summary(_sup_summaries, global_step=_global_step)

        # Increment step counter, check if maximum iterations per epoch is achieved and stop in that case
        _global_step = sess.run(increment_global_step)

        if _global_step - _init_step > model_config["epoch_it"]:
            run = False
            print("Finished training phase, stopping batch generators")
            sup_batch_gen.stop_workers()

    # Epoch finished - Save model
    print("Finished epoch!")
    save_path = saver.save(sess, model_config["model_base_dir"] + os.path.sep + str(experiment_id) + os.path.sep + str(experiment_id), global_step=int(_global_step))

    # Close session, clear computational graph
    writer.flush()
    writer.close()
    sess.close()
    tf.reset_default_graph()

    return save_path

@ex.capture
def optimise(model_config, experiment_id, dataset):
    epoch = 0
    best_loss = 10000
    model_path = None
    best_model_path = None
    for i in range(2):
        worse_epochs = 0
        if i==1:
            print("Finished first round of training, now entering fine-tuning stage")
            model_config["batch_size"] *= 2
            model_config["cache_size"] *= 2
            model_config["min_replacement_rate"] *= 2
            model_config["init_sup_sep_lr"] = 1e-5
        while worse_epochs < 20:    # Early stopping on validation set after a few epochs
            print("EPOCH: " + str(epoch))
            musdb_train, musdb_eval = dataset[0], dataset[1]
            model_path = train(sup_dataset=musdb_train, load_model=model_path)
            curr_loss = Test.test(model_config, model_folder=str(experiment_id), audio_list=musdb_eval, load_model=model_path)
            epoch += 1
            if curr_loss < best_loss:
                worse_epochs = 0
                print("Performance on validation set improved from " + str(best_loss) + " to " + str(curr_loss))
                best_model_path = model_path
                best_loss = curr_loss
            else:
                worse_epochs += 1
                print("Performance on validation set worsened to " + str(curr_loss))
    print("TRAINING FINISHED - TESTING WITH BEST MODEL " + best_model_path)
    test_loss = Test.test(model_config, model_folder=str(experiment_id), audio_list=musdb_eval, load_model=best_model_path)
    return best_model_path, test_loss

@ex.automain
def dsd_100_experiment(model_config):
    print("SCRIPT START")
    # Create subfolders if they do not exist to save results
    for dir in [model_config["model_base_dir"], model_config["log_dir"]]:
        if not os.path.exists(dir):
            os.makedirs(dir)

    print("Creating datasets")
    musdb_train, musdb_eval = [musdb_input.MusDBInput(
        is_training=is_training,
        data_dir=model_config['musdb_path'],
        transpose_input=False,
        use_bfloat16=False) for is_training in [True, False]]

    # Optimize in a +supervised fashion until validation loss worsens
    sup_model_path, sup_loss = optimise(dataset=[musdb_train, musdb_eval])
    print("Supervised training finished! Saved model at " + sup_model_path + ". Performance: " + str(sup_loss))
    Evaluate.produce_source_estimates(model_config, sup_model_path, model_config["musdb_path"], model_config["estimates_path"], "train")
