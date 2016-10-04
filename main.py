import numpy as np
from keras.models import Sequential, load_model, Model
from keras.layers import Convolution2D, Dense, Flatten, merge, MaxPooling2D, Input, AveragePooling2D, Lambda, Merge, Activation, Embedding
from keras.layers.recurrent import LSTM, GRU
from keras.layers.wrappers import TimeDistributed
from keras.optimizers import adam, rmsprop
from keras.layers.core import RepeatVector, Masking, TimeDistributedDense
from keras.initializations import uniform
from keras.preprocessing.sequence import pad_sequences
from keras import backend as K
from vizdoom import *
import scipy.ndimage
from time import sleep
import matplotlib.pyplot as plt
import itertools as it
import datetime
from enum import Enum


image_height, image_width = 60, 80

def display_state(state):
    frames = state.shape[0]
    for frame in range(frames):
        plt.subplot(1, frames, frame+1)
        plt.imshow(state[frame], cmap='Greys_r')
    plt.show()


class Mode(Enum):
    TRAIN = 1
    TEST = 2
    DISPLAY = 3

class Level(Enum):
    BASIC = "configs/basic.cfg"
    HEALTH = "configs/health_gathering.cfg"
    DEATHMATCH = "configs/deathmatch.cfg"
    DEFEND = "configs/defend_the_center.cfg"
    WAY_HOME = "configs/my_way_home.cfg"

class Algorithm(Enum):
    DQN = 1
    DDQN = 2
    DRQN = 3

class Architecture(Enum):
    DIRECT = 1
    DUELING = 2
    SEQUENCE = 3

class ExplorationPolicy(Enum):
    E_GREEDY = 1
    SOFTMAX = 2
    SHIFTED_MULTINOMIAL = 3

class MaskedEmbedding(Embedding):
    def __init__(self, mask_value=0, **kwargs):
        self.mask_value=mask_value
        super(MaskedEmbedding, self).__init__(**kwargs)
        self.mask_zero = True

    def compute_mask(self, x, mask=None):
        return K.not_equal(x, self.mask_value)


class Environment(object):
    def __init__(self, level = Level.BASIC, combine_actions = False, visible = True):
        self.game = DoomGame()
        self.game.load_config(level.value)
        self.game.set_window_visible(visible)
        self.game.init()
        self.actions_num = self.game.get_available_buttons_size()
        self.combine_actions = combine_actions
        self.actions = []
        if self.combine_actions:
            for perm in it.product([False, True], repeat=self.actions_num):
                self.actions.append(list(perm))
        else:
            for action in range(self.actions_num):
                one_hot = [False] * self.actions_num
                one_hot[action] = True
                self.actions.append(one_hot)
        self.screen_width = self.game.get_screen_width()
        self.screen_height = self.game.get_screen_height()

    def step(self, action):
        reward = self.game.make_action(action)
        next_state = self.game.get_state().image_buffer
        game_over = self.game.is_episode_finished()
        return next_state, reward, game_over

    def get_curr_state(self):
        return self.game.get_state().image_buffer

    def new_episode(self):
        self.game.new_episode()

    def is_game_over(self):
        return self.game.is_episode_finished()


class Agent(object):
    def __init__(self, discount, level, algorithm, prioritized_experience, max_memory, exploration_policy,
                 learning_rate, history_length, batch_size, combine_actions, target_update_freq, epsilon_start, epsilon_end,
                 epsilon_annealing_steps, temperature=10, snapshot='', train=True, visible=True, skipped_frames=4,
                 architecture=Architecture.DIRECT, max_action_sequence_length=1):

        self.trainable = train

        # e-greedy policy
        self.epsilon_annealing_steps = epsilon_annealing_steps #steps
        self.epsilon_start = epsilon_start
        self.epsilon_end = epsilon_end
        if self.trainable:
            self.epsilon = self.epsilon_start
        else:
            self.epsilon = self.epsilon_end

        # softmax / multinomial policy
        self.average_minimum = 0 # for multinomial policy
        self.temperature = temperature

        self.policy = exploration_policy

        # initialization
        self.environment = Environment(level=level, combine_actions=combine_actions, visible=visible)
        self.memory = ExperienceReplay(max_memory=max_memory, prioritized=prioritized_experience, store_episodes=(architecture==Architecture.SEQUENCE))
        self.preprocessed_curr = []
        self.win_count = 0
        self.curr_step = 0

        self.state_width = image_width
        self.state_height = image_height
        self.scale = self.state_width / float(self.environment.screen_width)

        # recurrent
        self.max_action_sequence_length = max_action_sequence_length
        self.num_actions = len(self.environment.actions)
        self.input_action_space_size = self.num_actions + 2 # number of actions + start and end tokens
        self.output_action_space_size = self.num_actions + 1
        self.start_token = self.num_actions + 1
        self.end_token = self.num_actions

        # training
        self.discount = discount
        self.history_length = history_length # should be 1 for DRQN
        self.skipped_frames = skipped_frames
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.target_update_freq = target_update_freq

        self.algorithm = algorithm
        self.architecture = architecture

        self.target_network = self.create_network(architecture=architecture, algorithm=algorithm)
        self.online_network = self.create_network(architecture=architecture, algorithm=algorithm)
        if snapshot != '':
            print("loading snapshot " + str(snapshot))
            self.target_network.load_weights(snapshot)
            self.online_network.load_weights(snapshot)
            self.target_network.compile(adam(lr=self.learning_rate), "mse")
            self.online_network.compile(adam(lr=self.learning_rate), "mse")

    def create_network(self, architecture=Architecture.DIRECT, algorithm=Algorithm.DDQN):
        if algorithm == Algorithm.DRQN:
            network_type = "recurrent"
        else:
            network_type = "sequential"

        if architecture == Architecture.DIRECT:
            if network_type == "inception":
                print("Built an inception DQN")
                input_img = Input(shape=(self.history_length, self.state_height, self.state_width))
                tower_1 = Convolution2D(16, 1, 1, border_mode='same', activation='relu')(input_img)
                tower_1 = Convolution2D(16, 3, 3, border_mode='same', activation='relu')(tower_1)
                tower_2 = Convolution2D(16, 1, 1, border_mode='same', activation='relu')(input_img)
                tower_2 = Convolution2D(16, 5, 5, border_mode='same', activation='relu')(tower_2)
                tower_3 = MaxPooling2D((3, 3), strides=(1, 1), border_mode='same')(input_img)
                tower_3 = Convolution2D(16, 1, 1, border_mode='same', activation='relu')(tower_3)
                output1 = merge([tower_1, tower_2, tower_3], mode='concat', concat_axis=1)
                avgpool = AveragePooling2D((7, 7), strides=(8, 8))(output1)
                flatten = Flatten()(avgpool)
                output = Dense(len(self.environment.actions))(flatten)
                model = Model(input=input_img, output=output)
                model.compile(rmsprop(lr=self.learning_rate), "mse")
                #model.summary()
            elif network_type == "sequential":
                print("Built a sequential DQN")
                model = Sequential()
                model.add(Convolution2D(16, 3, 3, subsample=(2,2), activation='relu', input_shape=(self.history_length, self.state_height, self.state_width), init='uniform', trainable=True))
                model.add(Convolution2D(32, 3, 3, subsample=(2,2), activation='relu', init='uniform', trainable=True))
                model.add(Convolution2D(64, 3, 3, subsample=(2,2), activation='relu', init='uniform', trainable=True))
                model.add(Convolution2D(128, 3, 3, subsample=(1,1), activation='relu', init='uniform'))
                model.add(Convolution2D(256, 3, 3, subsample=(1,1), activation='relu', init='uniform'))
                model.add(Flatten())
                model.add(Dense(512, activation='relu', init='uniform'))
                model.add(Dense(len(self.environment.actions),init='uniform'))
                model.compile(rmsprop(lr=self.learning_rate), "mse")
            elif network_type == "recurrent":
                print("Built a recurrent DQN")
                model = Sequential()
                model.add(TimeDistributed(Convolution2D(16, 3, 3, subsample=(2,2), activation='relu', init='uniform', trainable=True),input_shape=(self.history_length, 1, self.state_height, self.state_width)))
                model.add(TimeDistributed(Convolution2D(32, 3, 3, subsample=(2,2), activation='relu', init='uniform', trainable=True)))
                model.add(TimeDistributed(Convolution2D(64, 3, 3, subsample=(2,2), activation='relu', init='uniform', trainable=True)))
                model.add(TimeDistributed(Convolution2D(128, 3, 3, subsample=(1,1), activation='relu', init='uniform')))
                model.add(TimeDistributed(Convolution2D(256, 3, 3, subsample=(1,1), activation='relu', init='uniform')))
                model.add(TimeDistributed(Flatten()))
                model.add(LSTM(512, activation='relu', init='uniform', unroll=True))
                model.add(Dense(len(self.environment.actions),init='uniform'))
                model.compile(rmsprop(lr=self.learning_rate), "mse")
                #model.summary()
        elif architecture == Architecture.DUELING:
            if network_type == "sequential":
                print("Built a dueling sequential DQN")
                input = Input(shape=(self.history_length, self.state_height, self.state_width))
                x = Convolution2D(16, 3, 3, subsample=(2, 2), activation='relu',
                    input_shape=(self.history_length, image_height, image_width), init='uniform',
                    trainable=True)(input)
                x = Convolution2D(32, 3, 3, subsample=(2, 2), activation='relu', init='uniform', trainable=True)(x)
                x = Convolution2D(64, 3, 3, subsample=(2, 2), activation='relu', init='uniform', trainable=True)(x)
                x = Convolution2D(128, 3, 3, subsample=(1, 1), activation='relu', init='uniform')(x)
                x = Convolution2D(256, 3, 3, subsample=(1, 1), activation='relu', init='uniform')(x)
                x = Flatten()(x)
                # state value tower - V
                state_value = Dense(256, activation='relu', init='uniform')(x)
                state_value = Dense(1, init='uniform')(state_value)
                state_value = Lambda(lambda s: K.expand_dims(s[:, 0], dim=-1), output_shape=(len(self.environment.actions),))(state_value)
                # action advantage tower - A
                action_advantage = Dense(256, activation='relu', init='uniform')(x)
                action_advantage = Dense(len(self.environment.actions), init='uniform')(action_advantage)
                action_advantage = Lambda(lambda a: a[:, :] - K.mean(a[:, :], keepdims=True), output_shape=(len(self.environment.actions),))(action_advantage)
                # merge to state-action value function Q
                state_action_value = merge([state_value, action_advantage], mode='sum')
                model = Model(input=input, output=state_action_value)
                model.compile(rmsprop(lr=self.learning_rate), "mse")
                #model.summary()
            else:
                print("ERROR: not implemented")
                exit()
        elif architecture == Architecture.SEQUENCE:
            # state model
            state_model = Sequential()
            state_model.add(Convolution2D(16, 3, 3, subsample=(2, 2), activation='relu',
                                          input_shape=(self.history_length, self.state_height, self.state_width), init='uniform', trainable=True))
            state_model.add(
                Convolution2D(32, 3, 3, subsample=(2, 2), activation='relu', init='uniform', trainable=True))
            state_model.add(
                Convolution2D(64, 3, 3, subsample=(2, 2), activation='relu', init='uniform', trainable=True))
            state_model.add(Convolution2D(128, 3, 3, subsample=(1, 1), activation='relu', init='uniform'))
            state_model.add(Convolution2D(256, 3, 3, subsample=(1, 1), activation='relu', init='uniform'))
            state_model.add(Flatten())
            state_model.add(Dense(100, activation='relu', init='uniform'))
            state_model.add(RepeatVector(1))
            #state_model.summary()

            # action model
            action_model = Sequential()
            #action_model.add(Masking(mask_value=8, input_shape=(self.max_action_sequence_length,1)))
            action_model.add(MaskedEmbedding(self.end_token, input_dim=self.input_action_space_size, output_dim=100, init='uniform', input_length=(self.max_action_sequence_length+1)))
            #action_model.summary()

            # combined model
            model = Sequential()
            model.add(Merge([state_model, action_model], mode='concat', concat_axis=1))
            model.add(LSTM(256, return_sequences=True, init='uniform'))
            model.add(TimeDistributed(Dense(self.output_action_space_size, init='uniform', activation='softmax')))
            #model.summary()
            #exit()
            model.compile(loss='categorical_crossentropy', optimizer='rmsprop')

        return model

    def preprocess(self, state):
        # resize image and convert to greyscale
        if self.scale == 1:
            return np.mean(state,0)
        else:
            return scipy.misc.imresize(np.mean(state,0), self.scale)

    def get_inputs_and_targets(self, minibatch):
        """Given a minibatch, extract the inputs and targets for the training according to DQN or DDQN

        :param minibatch: the minibatch to train on
        :return: the inputs, targets and sample weights (for prioritized experience replay)
        """
        targets = list()
        action_idxs = list()
        inputs = list()
        samples_weights = list()
        for idx, transition_list, game_over, sample_weight in minibatch:
            # for episodic experience - choose a random ending action
            end_transition_idx = np.random.randint(0, len(transition_list))
            start_transition_idx = max(0, end_transition_idx-self.max_action_sequence_length+1)
            first_transition = transition_list[start_transition_idx]
            last_transition = transition_list[end_transition_idx]
            TD_error = 0 # temporal difference error (actual action-value - predicted action value)
            inputs.append(first_transition.preprocessed_curr[0])

            # get the sequence of actions for episodic experience replay
            # take actions starting from one action before the first state and ending in one before the last state
            record_action_idxs = [self.start_token]
            chosen_transitions = transition_list[max(0,start_transition_idx):max(0,end_transition_idx)]
            for transition in chosen_transitions:
                record_action_idxs += [transition.action]
            padded_action_sequence_idxs = pad_sequences([record_action_idxs],
                                                        maxlen=(self.max_action_sequence_length+1),
                                                        value=self.end_token,
                                                        padding='post')
            action_idxs += list(padded_action_sequence_idxs)

            # prepare input for predicting the current actions
            curr_input = first_transition.preprocessed_curr
            if self.architecture == Architecture.SEQUENCE:
                curr_input = [first_transition.preprocessed_curr, padded_action_sequence_idxs]

            # prepare input for predicting the next actions
            next_input = last_transition.preprocessed_next
            if self.architecture == Architecture.SEQUENCE:
                # TODO: maybe need to look at next episode
                padded_action_sequence_idxs = pad_sequences([np.array([self.start_token])],
                                                            maxlen=(self.max_action_sequence_length+1),
                                                            value=self.end_token,
                                                            padding='post')
                next_input = [last_transition.preprocessed_next, padded_action_sequence_idxs]

            # get the current action-values
            target = self.online_network.predict(curr_input)[0]
            current_target_idx = end_transition_idx+1
            chosen_transitions += [last_transition]
            # calculate TD-target for last transition
            if game_over:
                TD_target = last_transition.reward
            else:
                if self.algorithm == Algorithm.DQN:
                    Q_sa = self.target_network.predict(next_input)
                    if self.architecture == Architecture.SEQUENCE:
                        Q_sa = Q_sa[0][current_target_idx]
                    TD_target = last_transition.reward + self.discount * np.max(Q_sa)
                elif self.algorithm == Algorithm.DDQN:
                    if self.architecture == Architecture.SEQUENCE:
                        best_next_action = np.argmax(self.online_network.predict(next_input)[0][1])
                        Q_sa = self.target_network.predict(next_input)[0][1][best_next_action]
                    else:
                        best_next_action = np.argmax(self.online_network.predict(next_input))
                        Q_sa = self.target_network.predict(next_input)[0][best_next_action]
                    TD_target = last_transition.reward + self.discount * Q_sa

            if self.architecture == Architecture.SEQUENCE:
                target[current_target_idx][last_transition.action] = TD_target
                # bootstrap the targets of all the other actions
                for idx in range(current_target_idx - 1, 0, -1):
                    TD_target = chosen_transitions[idx].reward + self.discount * TD_target
                    target[idx][chosen_transitions[idx].action] = TD_target
            else:
                TD_error = TD_target - target[last_transition.action]
                target[last_transition.action] = TD_target
            targets.append(target)

            # updates priority and weight for prioritized experience replay
            if self.memory.prioritized:
                self.memory.update_transition_priority(idx, np.abs(TD_error))
                samples_weights.append(sample_weight)

        return np.array(inputs), np.array(targets), np.array(samples_weights), np.array(action_idxs)

    def softmax_selection(self, Q):
        """Select the action according to the softmax exploration policy

        :param Q: the Q values for the current state
        :return: the action and the action index
        """
        # compute thresholds and choose a random number
        exp_Q = np.array(np.exp(Q/float(self.temperature)), copy=True)
        prob = np.random.rand(1)
        importances = [action_value/float(np.sum(exp_Q)) for action_value in exp_Q]
        thresholds = np.cumsum(importances)
        # multinomial sampling according to priorities
        for action_idx, threshold in zip(range(len(thresholds)), thresholds):
            if prob < threshold:
                action = self.environment.actions[action_idx]
                return action, action_idx
        return self.environment.actions[len(exp_Q)-1], len(exp_Q)-1

    def shifted_multinomial_selection(self, Q):
        """Select the action according to a shifted multinomial sampling policy

        :param Q: the Q values of the current state
        :return: the action and the action index
        """
        # Q values are shifted so that we won't have negative values
        self.average_minimum = 0.95 * self.average_minimum + 0.05 * np.min(Q)
        shifted_Q = np.array(Q - min(self.average_minimum, np.min(Q)), copy=True)
        # compute thresholds and choose a random number
        prob = np.random.rand(1)
        importances = [action_value/float(np.sum(shifted_Q)) for action_value in shifted_Q]
        thresholds = np.cumsum(importances)
        # multinomial sampling according to priorities
        for action_idx, threshold in zip(range(len(thresholds)), thresholds):
            if prob < threshold:
                action = self.environment.actions[action_idx]
                return action, action_idx

    def e_greedy(self, Q):
        """ Select the action according to the e-greedy exploration policy

        :param Q: the Q values for the current state
        :return: the action and the action index
        """
        # choose action randomly or greedily
        coin_toss = np.random.rand(1)[0]
        if coin_toss > self.epsilon:
            action_idx = np.argmax(Q)
        else:
            action_idx = np.random.randint(len(self.environment.actions))
        action = self.environment.actions[action_idx]

        # anneal epsilon value
        if self.epsilon > self.epsilon_end:
            self.epsilon -= float(self.epsilon_start - self.epsilon_end)/float(self.epsilon_annealing_steps)

        return action, action_idx

    def get_action_according_to_exploration_policy(self, Q):
        action, action_idx = self.environment.actions[0], 0
        if self.policy == ExplorationPolicy.E_GREEDY:
            action, action_idx = self.e_greedy(Q)
        elif self.policy == ExplorationPolicy.SHIFTED_MULTINOMIAL:
            action, action_idx = self.shifted_multinomial_selection(Q)
        elif self.policy == ExplorationPolicy.SOFTMAX:
            action, action_idx = self.softmax_selection(Q)
        else:
            print("Error: exploration policy not available")
            exit()
        return action, action_idx

    def predict(self):
        """predict action according to the current state

        :return: the action, the action index, the mean Q value
        """
        # if no current state is present, create one by stacking the duplicated current state
        if self.preprocessed_curr == []:
            frame = self.environment.get_curr_state()
            preprocessed_frame = self.preprocess(frame)
            for t in range(self.history_length):
                self.preprocessed_curr.append(preprocessed_frame)

        # choose action
        preprocessed_curr = np.reshape(self.preprocessed_curr, (1, self.history_length, self.state_height, self.state_width))
        if self.algorithm == Algorithm.DRQN:
            # expand dims to have a time dimension + switch between depth and time
            preprocessed_curr = np.expand_dims(preprocessed_curr, axis=0).transpose(0,2,1,3,4)

        #display_state(preprocessed_curr[0])
        if self.architecture == Architecture.SEQUENCE:
            # predict an action sequence
            action_sequence_idxs = np.array([self.start_token])
            action_sequence = []
            Qs = []
            for i in range(self.max_action_sequence_length):
                padded_action_sequence_idxs = pad_sequences([action_sequence_idxs],
                                                            maxlen=(self.max_action_sequence_length+1),
                                                            value=self.end_token,
                                                            padding='post')
                #print(padded_action_sequence_idxs)
                Q = self.online_network.predict([preprocessed_curr, padded_action_sequence_idxs], batch_size=1)
                Q = Q[0][len(action_sequence_idxs)]
                #print(preprocessed_curr)
                #print(self.online_network.predict([preprocessed_curr, padded_action_sequence_idxs], batch_size=1))
                # stop if end token was sampled
                #print(Q)
                if np.argmax(Q) == self.end_token:
                    break
                action, action_idx = self.get_action_according_to_exploration_policy(Q)
                action_sequence += [action]
                action_sequence_idxs = np.append(action_sequence_idxs, [action_idx])
                Qs += [np.mean(Q)]
            if Qs == []:
                action_sequence_idxs = np.array([self.start_token, 0])
                action_sequence = [self.environment.actions[0], self.environment.actions[0]]
                meanQ = 0 #TODO: this is wrong
            else:
                meanQ = np.mean(Qs)

            return action_sequence, action_sequence_idxs[1:], meanQ
        else:
            # predict a single action

            Q = self.online_network.predict(preprocessed_curr, batch_size=1)
            action, action_idx = self.get_action_according_to_exploration_policy(Q)

            return [action], [action_idx], np.mean(Q) # send as a list of actions to conform with episodic experience replay

    def step(self, action, action_idx):
        # repeat action several times and stack the first frame onto the previous state
        reward = 0
        game_over = False
        preprocessed_next = list(self.preprocessed_curr)
        del preprocessed_next[0]
        for t in range(self.skipped_frames):
            frame, r, game_over = self.environment.step(action)
            reward += r # reward is accumulated
            if game_over:
                break
            if t == self.skipped_frames-1: # rest are skipped
                preprocessed_next.append(self.preprocess(frame))

        # episode finished
        if game_over:
            preprocessed_next = []
            self.environment.new_episode()
            if reward > 0:
                self.win_count += 1 # irrelevant to most levels

        return preprocessed_next, reward, game_over

    def store_next_state(self, preprocessed_next, reward, game_over, action_idx):
        preprocessed_curr = np.reshape(self.preprocessed_curr, (1, self.history_length, image_height, image_width))
        self.preprocessed_curr = list(preprocessed_next) # saved as list
        if preprocessed_next != []:
            preprocessed_next = np.reshape(preprocessed_next, (1, self.history_length, image_height, image_width))

        # store transition
        self.memory.remember(Transition(preprocessed_curr, action_idx, reward, preprocessed_next), game_over) # stored as np array

        self.curr_step += 1

        # update target network with online network once in a while
        if self.curr_step % self.target_update_freq == 0:
            print(">>> update the target")
            self.target_network.set_weights(self.online_network.get_weights())

        return reward, game_over

    def train(self):
        """Train the online network on a minibatch

        :return: the train loss
        """
        #print("getting batch")
        minibatch = self.memory.sample_minibatch(self.batch_size)
        inputs, targets, samples_weights, action_idxs = self.get_inputs_and_targets(minibatch)
        #print("training")
        if self.memory.prioritized:
            return self.online_network.train_on_batch(inputs, targets, sample_weight=samples_weights)
        elif self.architecture == Architecture.SEQUENCE: # episodic
            return self.online_network.train_on_batch([inputs, action_idxs], targets)
        else:
            return self.online_network.train_on_batch(inputs, targets)

class Transition(object):
    def __init__(self, preprocessed_curr, action, reward, preprocessed_next):
        self.preprocessed_curr = preprocessed_curr
        self.action = action
        self.reward = reward
        self.preprocessed_next = preprocessed_next

class MemoryRecord(object):
    def __init__(self, transition_list=[], game_over=False, transition_powered_priority=1):
        self.transition_list = transition_list
        self.transition_powered_priority = transition_powered_priority
        self.game_over = game_over
        self.is_closed = False

    def add_transition(self, transition, game_over, transition_powered_priority=1):
        # add a single transition to a record and update the game over state
        if self.is_closed:
            raise Exception('record finalized')
        self.transition_list += [transition]
        self.transition_powered_priority = transition_powered_priority
        self.game_over = game_over

    def finalize(self):
        # finalize the record so no more transitions will be added
        self.is_closed = True

class ExperienceReplay(object):
    # memory consists of tuples [transition, game_over, priority^alpha]
    def __init__(self, max_memory=50000, prioritized=False, store_episodes=False):
        # experience replay structure params
        self.max_memory = max_memory
        self.memory = []
        self.store_episodes = store_episodes

        # prioritized experience replay params
        self.prioritized = prioritized
        self.alpha = 0.6 # prioritization factor
        self.beta_start = 0.4
        self.beta_end = 1
        self.beta = self.beta_end
        self.sum_powered_priorities = 0 # sum p^alpha

    def is_last_record_closed(self):
        return self.memory == [] or self.memory[-1].is_closed == True

    def add_record(self, transition, game_over, transition_powered_priority):
        record = MemoryRecord([transition], game_over, transition_powered_priority)
        self.memory.append(record)

    def get_last_record(self):
        return self.memory[-1]

    def remember(self, transition, game_over):
        """Add a transition to the experience replay

        :param transition: the transition to insert
        :param game_over: is the next state a terminal state?
        """
        # set the priority to the maximum current priority
        transition_powered_priority = 1e-7 ** self.alpha
        if self.prioritized:
            transition_powered_priority = np.max(self.memory,1)[0,2]
        self.sum_powered_priorities += transition_powered_priority

        # store transition
        if self.is_last_record_closed():
            self.add_record(transition, game_over, transition_powered_priority)
        else:
            self.get_last_record().add_transition(transition, game_over, transition_powered_priority)
        # finalize the record if necessary
        if not self.store_episodes or (self.store_episodes and (game_over or transition.reward > 0 or True)): #TODO: this is wrong
            self.get_last_record().finalize()

        # free some space (delete the oldest transition or episode)
        if len(self.memory) > self.max_memory:
            if self.prioritized:
                if self.store_episodes:
                    self.sum_powered_priorities -= np.sum(np.array(self.memory)[0,:,2])
                else:
                    self.sum_powered_priorities -= self.memory[0].transition_powered_priority
            del self.memory[0]

    def sample_minibatch(self, batch_size, not_terminals=False):
        """Samples one minibatch of transitions from the experience replay

        :param batch_size: the minibatch size
        :param not_terminals: sample or don't sample transitions were the next state is a terminal state
        :return: a list of tuples of the form: [idx, transition, game_over, weight]
        """
        batch_size = min(len(self.memory), batch_size)
        if self.prioritized: # TODO: not currently working for episodic experience replay
            # prioritized experience replay
            probs = np.random.rand(batch_size)
            importances = [self.get_transition_importance(idx) for idx in range(len(self.memory))]
            thresholds = np.cumsum(importances)

            # multinomial sampling according to priorities
            indices = []
            for p in probs:
                for idx, threshold in zip(range(len(thresholds)), thresholds):
                    if p < threshold:
                        indices += [idx]
                        break
        else:
            indices = np.random.choice(len(self.memory), batch_size)

        minibatch = list()
        for idx in indices:
            while not_terminals and self.memory[idx].game_over:
                idx = np.random.choice(len(self.memory), 1)[0]
            weight = 0
            if self.prioritized: # TODO: not working for episodic experience replay
                weight = self.get_transition_weight(idx)
            minibatch.append([idx, self.memory[idx].transition_list, self.memory[idx].game_over, weight])  # idx, [transition, transition, ...] , game_over, weight

        if self.prioritized:
            max_weight = np.max(minibatch,0)[3]
            for idx in range(len(minibatch)):
                minibatch[idx][3] /= float(max_weight) # normalize weights relative to the minibatch

        #print(minibatch)
        return minibatch

    def update_transition_priority(self, transition_idx, priority):
        """Update the priority of a transition by its index

        :param transition_idx: the index of the transition
        :param priority: the new priority
        """
        self.sum_powered_priorities -= self.memory[transition_idx].transition_powered_priority
        powered_priority = (priority+np.spacing(0)) ** self.alpha
        self.sum_powered_priorities += powered_priority
        self.memory[transition_idx].transition_powered_priority = powered_priority

    def get_transition_importance(self, transition_idx):
        """Get the importance of a transition by its index

        :param transition_idx: the index of the transition
        :return: the importance - priority^alpha/sum(priority^alpha)
        """
        powered_priority = self.memory[transition_idx].transition_powered_priority
        importance = powered_priority / float(self.sum_powered_priorities)
        return importance

    def get_transition_weight(self, transition_idx):
        """Get the weight of a transition by its index

        :param transition_idx: the index of the transition
        :return: the weight of the transition - 1/(importance*N)^beta
        """
        weight = 1/float(self.get_transition_importance(transition_idx)*self.max_memory)**self.beta
        return weight


class Entity(object):
    def __init__(self, agents_args_list, entity_args):
        self.agents = []
        for args in agents_args_list:
            agent = Agent(algorithm=args["algorithm"],
                          discount=args["discount"],
                          snapshot=args["snapshot"],
                          max_memory=args["max_memory"],
                          prioritized_experience=args["prioritized_experience"],
                          exploration_policy=args["exploration_policy"],
                          learning_rate=args["learning_rate"],
                          level=args["level"],
                          history_length=args["history_length"],
                          batch_size=args["batch_size"],
                          temperature=args["temperature"],
                          combine_actions=args["combine_actions"],
                          train=(args["mode"] == Mode.TRAIN),
                          skipped_frames=args["skipped_frames"],
                          visible=False,
                          target_update_freq=args["target_update_freq"],
                          epsilon_start=args["epsilon_start"],
                          epsilon_end=args["epsilon_end"],
                          epsilon_annealing_steps=args["epsilon_annealing_steps"])

            if (args["mode"] == Mode.TEST or args["mode"] == Mode.DISPLAY) and args["snapshot"] == '':
                print("Warning: mode set to " + str(args["mode"]) + " but no snapshot was loaded")

            self.agents += [agent]
        self.episodes = entity_args["episodes"]
        self.steps_per_episode = entity_args["steps_per_episode"]
        self.mode = entity_args["mode"]
        self.start_learning_after = entity_args["start_learning_after"]
        self.average_over_num_episodes = entity_args["average_over_num_episodes"]
        self.snapshot_episodes = entity_args["snapshot_episodes"]
        self.environment = Environment(level=entity_args["level"], combine_actions=entity_args["combine_actions"])
        self.history_length = entity_args["history_length"]
        self.win_count = 0
        self.curr_step = 0

    def combine_actions(self, aiming_actions, exploring_actions):
        # aiming_actions (defend_the_center) = 1. TURN_LEFT, 2. TURN_RIGHT, 3. ATTACK
        # exploring_actions (health_gathering or my_way_home) = 1. TURN_LEFT, 2. TURN_RIGHT, 3. MOVE_FORWARD, 4. MOVE_LEFT, 5. MOVE_RIGHT
        # death match actions (deathmatch) = 1. ATTACK, 2. SPEED, 3. STRAFE, 4. MOVE_RIGHT, 5. MOVE_LEFT, 6. MOVE_BACKWARD, 7. MOVE_FORWARD,
        #                       8. TURN_RIGHT, 9. TURN_LEFT, 10. SELECT_WEAPON1, 11. SELECT_WEAPON2, 12. SELECT_WEAPON3,
        #                       13. SELECT_WEAPON4, 14. SELECT_WEAPON5, 15. SELECT_WEAPON6, 16. SELECT_NEXT_WEAPON,
        #                       17. SELECT_PREV_WEAPON, 18. LOOK_UP_DOWN_DELTA, 19. TURN_LEFT_RIGHT_DELTA, 20. MOVE_LEFT_RIGHT_DELTA

        actions = [False] * 20
        actions[0] = aiming_actions[2]      # attack
        actions[3] = exploring_actions[4]   # move right
        actions[4] = exploring_actions[3]   # move left
        actions[6] = exploring_actions[2]   # move forward
        actions[7] = aiming_actions[1] or exploring_actions[1]  # turn right
        actions[8] = aiming_actions[0] or exploring_actions[0]  # turn left
        actions[11] = True # always use gun

        return actions

    def step(self, action):
        # repeat action several times and stack the states
        reward = 0
        game_over = False
        next_state = list()
        for t in range(self.history_length):
            s, r, game_over = self.environment.step(action)
            reward += r # reward is accumulated
            if game_over:
                break
            next_state.append(s)

        # episode finished
        if game_over:
            self.environment.new_episode()

        if reward > 0 and game_over:
            self.win_count += 1

        return next_state, reward, game_over

    def run(self):
        # initialize
        total_steps, average_return = 0, 0
        returns = []
        for i in range(self.episodes):
            self.environment.new_episode()
            steps, curr_return = 0, 0
            game_over = False
            while not game_over and steps < self.steps_per_episode:
                # each agent predicts the action it should do
                actions, action_idxs = [], []
                for agent in self.agents:
                    action, action_idx, _ = agent.predict()
                    actions += [action]
                    action_idxs += [action_idx]
                # the actions are combined together
                action = self.combine_actions(actions[0], actions[1]) #TODO: make this more generic
                # the entity performs the action
                next_state, reward, game_over = self.step(action)
                # each agent preprocesses the next state and stores it
                for agent_idx, agent in enumerate(self.agents):
                    agent.store_next_state(next_state, reward, game_over, action_idxs[agent_idx])

                steps += 1
                curr_return += reward

                # delay a bit so we humans can understand what we are seeing
                if self.mode == Mode.DISPLAY:
                    sleep(0.05)

                if i > self.start_learning_after and self.mode == Mode.TRAIN:
                    for agent in self.agents:
                        agent.train()

            # average results
            n = float(self.average_over_num_episodes)
            average_return = (1 - 1 / n) * average_return + (1 / n) * curr_return
            total_steps += steps
            returns += [average_return]

            # print progress
            print("")
            print(str(datetime.datetime.now()))
            print("episode = " + str(i) + " steps = " + str(total_steps))
            print("current_return = " + str(curr_return) + " average return = " + str(average_return))

            # save snapshot of target network
            if i % self.snapshot_episodes == self.snapshot_episodes - 1:
                for agent_idx, agent in enumerate(self.agents):
                    snapshot = 'agent' + str(agent_idx) + '_model_' + str(i + 1) + '.h5'
                    print(str(datetime.datetime.now()) + " >> saving snapshot to " + snapshot)
                    agent.target_network.save_weights(snapshot, overwrite=True)

        self.environment.game.close()
        return returns


def run_experiment(args):
    """ Run a single experiment, either train, test or display of an agent

    :param args: a dictionary containing all the parameters for the run
    :return: lists of average returns and mean Q values
    """
    agent = Agent(algorithm=args["algorithm"],
                  discount=args["discount"],
                  snapshot=args["snapshot"],
                  max_memory=args["max_memory"],
                  prioritized_experience=args["prioritized_experience"],
                  exploration_policy=args["exploration_policy"],
                  learning_rate=args["learning_rate"],
                  level=args["level"],
                  history_length=args["history_length"],
                  batch_size=args["batch_size"],
                  temperature=args["temperature"],
                  combine_actions=args["combine_actions"],
                  train=(args["mode"] == Mode.TRAIN),
                  skipped_frames=args["skipped_frames"],
                  target_update_freq=args["target_update_freq"],
                  epsilon_start=args["epsilon_start"],
                  epsilon_end=args["epsilon_end"],
                  epsilon_annealing_steps=args["epsilon_annealing_steps"],
                  architecture=args["architecture"],
                  visible=False,
                  max_action_sequence_length=args["max_action_sequence_length"])

    if (args["mode"] == Mode.TEST or args["mode"] == Mode.DISPLAY) and args["snapshot"] == '':
        print("Warning: mode set to " + str(args["mode"]) + " but no snapshot was loaded")

    n = float(args["average_over_num_episodes"])

    # initialize
    total_steps = 0
    returns_over_all_episodes = []
    mean_q_over_all_episodes = []
    return_buffer = []
    mean_q_buffer = []
    for i in range(args["episodes"]):
        agent.environment.new_episode()
        steps, curr_return, curr_Qs, loss = 0, 0, 0, 0
        game_over = False
        while not game_over and steps < args["steps_per_episode"]:
            #print("predicting")
            actions, action_idxs, mean_Q = agent.predict()
            #print("finished predicting")
            print(action_idxs)
            for action, action_idx in zip(actions, action_idxs):
                action_idx = int(action_idx)
                next_state, reward, game_over = agent.step(action, action_idx)
                agent.store_next_state(next_state, reward, game_over, action_idx)
                steps += 1
                total_steps += 1
                curr_return += reward
                curr_Qs += mean_Q

                # slow down things so we can see what's happening
                if args["mode"] == Mode.DISPLAY:
                    sleep(0.05)

                if i > args["start_learning_after"] and args["mode"] == Mode.TRAIN and total_steps % args["steps_between_train"] == 0:
                    loss += agent.train()
                    #print("finished training")
                if game_over or steps > args["steps_per_episode"]:
                    break

        # store stats
        if len(return_buffer) > n:
            del return_buffer[0]
        return_buffer += [curr_return]
        average_return = np.mean(return_buffer)

        if len(mean_q_buffer) > n:
            del mean_q_buffer[0]
        mean_q_buffer += [curr_Qs / float(steps)]
        average_mean_q = np.mean(mean_q_buffer)

        returns_over_all_episodes += [average_return]
        mean_q_over_all_episodes += [average_mean_q]

        print("")
        print(str(datetime.datetime.now()))
        print("episode = " + str(i) + " steps = " + str(total_steps))
        print("epsilon = " + str(agent.epsilon) + " loss = " + str(loss))
        print("current_return = " + str(curr_return) + " average return = " + str(average_return))

        # save snapshot of target network
        if i % args["snapshot_episodes"] == args["snapshot_episodes"] - 1:
            snapshot = 'model_' + str(i + 1) + '.h5'
            print(str(datetime.datetime.now()) + " >> saving snapshot to " + snapshot)
            agent.target_network.save_weights(snapshot, overwrite=True)

    agent.environment.game.close()
    return returns_over_all_episodes, mean_q_over_all_episodes


if __name__ == "__main__":
    experiment = "single_agent" # TODO: create a better way for this

    if experiment == "multi_agent":
        # multi agent entity

        aiming_agent = {
            "algorithm": Algorithm.DDQN,
            "discount": 0.99,
            "max_memory": 10000,
            "prioritized_experience": False,
            "exploration_policy": ExplorationPolicy.E_GREEDY,
            "learning_rate": 2.5e-4,
            "level": Level.DEFEND,
            "combine_actions": True,
            "temperature": 10,
            "batch_size": 10,
            "history_length": 4,
            "snapshot": 'defend_model_1000.h5',
            "mode": Mode.TRAIN,
            "skipped_frames": 4,
            "target_update_freq": 3000
        }

        exploring_agent = {
            "algorithm": Algorithm.DDQN,
            "discount": 0.99,
            "max_memory": 10000,
            "prioritized_experience": False,
            "exploration_policy": ExplorationPolicy.E_GREEDY,
            "learning_rate": 2.5e-4,
            "level": Level.HEALTH,
            "combine_actions": True,
            "temperature": 10,
            "batch_size": 10,
            "history_length": 4,
            "snapshot": 'health_model_500.h5',
            "mode": Mode.TRAIN,
            "skipped_frames": 4,
            "target_update_freq": 3000
        }

        entity_args = {
            "snapshot_episodes": 1000,
            "episodes": 2000,
            "steps_per_episode": 4000,  # 4300 for deathmatch, 300 for health gathering
            "average_over_num_episodes": 50,
            "start_learning_after": 200,
            "mode": Mode.TRAIN,
            "history_length": 4,
            "level": Level.DEATHMATCH,
            "combine_actions": True
        }

        entity = Entity([aiming_agent, exploring_agent], entity_args)
        returns = entity.run()

        plt.plot(range(len(returns)), returns, "r")
        plt.xlabel("episode")
        plt.ylabel("average return")
        plt.title("Average Return")

    elif experiment == "single_agent":
        lstm = {
            "snapshot_episodes": 100,
            "episodes": 1500,
            "steps_per_episode": 400,  # 4300 for deathmatch, 300 for health gathering
            "average_over_num_episodes": 50,
            "start_learning_after": 30,
            "algorithm": Algorithm.DDQN,
            "discount": 0.99,
            "max_memory": 5000,
            "prioritized_experience": False,
            "exploration_policy": ExplorationPolicy.SOFTMAX,
            "learning_rate": 2.5e-4,
            "level": Level.DEFEND,
            "combine_actions": True,
            "temperature": 10,
            "batch_size": 10,
            "history_length": 4,
            "snapshot": '',
            "mode": Mode.TRAIN,
            "skipped_frames": 7,
            "target_update_freq": 1000,
            "steps_between_train": 1,
            "epsilon_start": 0.7,
            "epsilon_end": 0.01,
            "epsilon_annealing_steps": 3e4,
            "architecture": Architecture.DIRECT
        }

        egreedy = {
            "snapshot_episodes": 100,
            "episodes": 300,
            "steps_per_episode": 4000, # 4300 for deathmatch, 300 for health gathering
            "average_over_num_episodes": 50,
            "start_learning_after": 10,
            "algorithm": Algorithm.DDQN,
            "discount": 0.99,
            "max_memory": 1000,
            "prioritized_experience": False,
            "exploration_policy": ExplorationPolicy.E_GREEDY,
            "learning_rate": 2.5e-4,
            "level": Level.DEFEND,
            "combine_actions": True,
            "temperature": 10,
            "batch_size": 10,
            "history_length": 4,
            "snapshot": '',
            "mode": Mode.TRAIN,
            "skipped_frames": 4,
            "target_update_freq": 1000,
            "steps_between_train": 1,
            "epsilon_start": 0.5,
            "epsilon_end": 0.01,
            "epsilon_annealing_steps": 3e4,
            "architecture": Architecture.DUELING,
            "max_action_sequence_length": 1
        }

        lstm = {
            "snapshot_episodes": 100,
            "episodes": 300,
            "steps_per_episode": 40, # 4300 for deathmatch, 300 for health gathering
            "average_over_num_episodes": 50,
            "start_learning_after": 10,
            "algorithm": Algorithm.DQN,
            "discount": 0.99,
            "max_memory": 1000,
            "prioritized_experience": False,
            "exploration_policy": ExplorationPolicy.E_GREEDY,
            "learning_rate": 2.5e-4,
            "level": Level.BASIC,
            "combine_actions": True,
            "temperature": 10,
            "batch_size": 10,
            "history_length": 4,
            "snapshot": '',
            "mode": Mode.TRAIN,
            "skipped_frames": 4,
            "target_update_freq": 1000,
            "steps_between_train": 1,
            "epsilon_start": 0.5,
            "epsilon_end": 0.01,
            "epsilon_annealing_steps": 3e4,
            "architecture": Architecture.SEQUENCE,
            "max_action_sequence_length": 1
        }

        runs = [lstm]

        colors = ["r", "g", "b"]
        for color, run in zip(colors, runs):
            # run agent
            returns, Qs = run_experiment(run)

            # plot results
            plt.figure(1)
            plt.plot(range(len(returns)), returns, color)
            plt.xlabel("episode")
            plt.ylabel("average return")
            plt.title("Average Return")

            plt.figure(2)
            plt.plot(range(len(Qs)), Qs, color)
            plt.xlabel("episode")
            plt.ylabel("mean Q value")
            plt.title("Mean Q Value")

        plt.show()
