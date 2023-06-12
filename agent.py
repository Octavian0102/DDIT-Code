import math

import pandas as pd

import environment as env
import config

import datetime as dt
from math import isclose

class Agent():
    """
    Models the agent and contains the algorithm for taking optimized actions
    """

    def __init__(self) -> None:
        self.market = env.Market()
        self.household = env.Household()

        self.time = dt.datetime.strptime(config.T_START, "%Y-%m-%d %H:%M:%S")

        # technical housekeeping variables
        self.length_forecast = 192 # 2 * 96 to observe future pv, load and battery data up to 2 days in advance
        self.index_f = 0 # current forecast index
        self.valid_f = 0 # forecast validity index

        # variables of which the agent has to keep track over time and maintain a forecast for the near future
        self.pv_forecast = [0] * self.length_forecast
        self.load_forecast = [0] * self.length_forecast
        self.battery_forecast = [config.BATTERY_CHARGE_INIT] * self.length_forecast
        self.discharge = [0] * self.length_forecast
        self.charge = [0] * self.length_forecast
        self.grid_demand = [0] * self.length_forecast
        self.grid_supply = [0] * self.length_forecast
        self.surplus_agg = [0] * self.length_forecast
        
        self.contracts = list() # a list of active contracts

        self.price_dict = dict() # initialize market price estimates with start prices
        self.price_dict["DA"] = [0] * 96 #self.market.prices_DA["Price"][0]
        self.price_dict["IA"] = [0] * 96 #self.market.prices_IA["Price"][0]
        self.price_dict["IC"] = [0] * 96 #self.market.prices_IC["Price"][0]

        # logging dataframes
        self.log_pd = pd.DataFrame(columns=["offer_DA", "offer_IA", "offer_IC", "grid_feedin", "battery_charge", "pv",
                                            "load", "Time"])
        self.action_log = pd.DataFrame(columns=["Time", "Market", "Price", "Quantity"])

        # violation counter for validation and debug purposes
        self.violations = 0


    def run(self) -> None:
        """
        Runs the optimization over the given time for the given environment
        """

        gains = dict()
        gains["grid"] = 0
        gains["DA"] = 0
        gains["IA"] = 0
        gains["IC"] = 0

        costs = 0

        # handle the situation in the first time step
        # here, no market offer is possible
        (load, pv, battery, _, _, _, _) = self.getForecasts(0)
        if(pv - load > 0): # if there is an energy surplus, use is to charge the battery or feed it into the grid
            if(battery + pv - load <= config.BATTERY_CHARGE_MAX): self.updateForecasts(0, pv - load, 0, 0, 0, 0)
            else: self.updateForecasts(0, 0, 0, 0, pv - load, 0)
        else: self.updateForecasts(0, 0, 0, load - pv, 0, 0) # satisfy a deficit from the grid

        for index in range(config.T):

            prices = self.market.getMarketPrices()

            delivered = 0 # cumulated energy quantity to deliver to the market in this time slot
            # check if contracts need to be fulfilled at the current time
            i = 0
            while i < len(self.contracts):
                (market, delivery_time, quantity, _) = self.contracts[i]

                if(delivery_time <= self.time): # if the contract is to be fulfilled now
                    #print(f"\tFulfilling contract for {delivery_time} at {market} of {quantity}, current time {self.time}")
                    gains[market] += prices[market] * quantity # obtain the money
                    delivered += quantity
                    self.action_log.loc[len(self.action_log)] = [self.time, market, prices[market], quantity] # log the action
                    self.contracts.pop(i) # delete the fulfilled contract from the list of open contracts
                    i -= 1
                i += 1

            # update running price average
            self.price_dict["DA"][(index + 48) % 96] = self.price_dict["DA"][(index + 48) % 96] * config.LAMBDA + prices["DA"] * (1 - config.LAMBDA)
            self.price_dict["IA"][(index + 48) % 96] = self.price_dict["IA"][(index + 48) % 96] * config.LAMBDA + prices["IA"] * (1 - config.LAMBDA)
            self.price_dict["IC"][(index + 48) % 96] = self.price_dict["IC"][(index + 48) % 96] * config.LAMBDA + prices["IC"] * (1 - config.LAMBDA)

            # determine the action to take using a greedy approach
            self.greedy()

            # check the validity of the action through different constraints
            # non-negativity
            if(self.charge[self.index_f] < 0):
                print(f"\t{index}: {self.time}: non-negativity charge: {self.charge[self.index_f]}")
                self.violations += 1
            if(self.discharge[self.index_f] < 0):
                print(f"\t{index}: {self.time}: non-negativity discharge: {self.discharge[self.index_f]}")
                self.violations += 1
            if(self.grid_demand[self.index_f] < 0):
                print(f"\t{index}: {self.time}: non-negativity grid_demand: {self.grid_demand[self.index_f]}")
                self.violations += 1
            if(self.grid_supply[self.index_f] < 0):
                print(f"\t{index}: {self.time}: non-negativity grid_supply: {self.grid_supply[self.index_f]}")
                self.violations += 1

            # battery state
            (load, pv, battery, _, _, _, _) = self.getForecasts(0)
            if(battery < config.BATTERY_CHARGE_MIN):
                print(f"\t{index}: {self.time}: battery minimum charge: {battery}")
                self.violations += 1
            if(battery > config.BATTERY_CHARGE_MAX):
                print(f"\t{index}: {self.time}: battery maximum charge: {battery}")
                self.violations += 1

            # only one of grid supply/demand and battery charge/discharge
            if(self.grid_demand[self.index_f] > 0 and self.grid_supply[self.index_f] > 0):
                print(f"\t{index}: {self.time}: grid supply and demand: {self.grid_supply[self.index_f]}; {self.grid_demand[self.index_f]}")
                self.violations += 1
            if(self.charge[self.index_f] > 0 and self.discharge[self.index_f] > 0):
                print(f"\t{index}: {self.time}: battery charge and discharge: {self.charge[self.index_f]}; {self.discharge[self.index_f]}")
                self.violations += 1
            
            # load balancing
            balance = pv + self.discharge[self.index_f] - self.charge[self.index_f] + \
                self.grid_demand[self.index_f] - self.grid_supply[self.index_f] - delivered - load
            if(not isclose(balance, 0, abs_tol = 0.000001)):
                print(f"\t{index}: {self.time}: load balance: {balance}")
                self.violations += 1

            #print(f"{balance} = {pv} + {self.discharge[self.index_f]} - {self.charge[self.index_f]} + " + \
            #      f"{self.grid_demand[self.index_f]} - {self.grid_supply[self.index_f]} - {delivered} - {load}\n")

            costs += self.grid_demand[self.index_f] * config.GRID_PRICE_RESIDENTIAL
            gains["grid"] += self.grid_supply[self.index_f] * config.GRID_PRICE_FEEDIN

            self.log_pd.loc[len(self.log_pd)] = [gains["DA"], gains["IA"], gains["IC"], gains["grid"],
                                                 battery, pv, load, self.time]
            
            self.updateHousekeeping()
            self.time = self.time + config.T_DELTA

        print(f"Constraint violations: {self.violations}")


    def greedy(self) -> None:
        """
        Decides what offers to place on the different markets, given the current market and household state
        """

        # call plan_decision for the different decision time points
        # plan decision for the next time step (IC market)
        self.plan_decision(1, ["IC"]) 

        # if the gate closure time for the intraday auction market is reached, plan decisions for the next day (IA and IC market)
        if(self.time.time() == dt.datetime.strptime(config.INTRADAY_AUCTION_CLOSURE, "%H:%M:%S").time()):
            for t in range(33, 33+96):
                self.plan_decision(t, ["IA"])
        # if the gate closure time for the day-ahead market is reached, plan decisions for the next day (DA, IA and IC market)
        if(self.time.time() == dt.datetime.strptime(config.DAY_AHEAD_CLOSURE, "%H:%M:%S").time()):
            for t in range(49, 49+96):
                self.plan_decision(t, ["DA"])

    def plan_decision(self, ahead_time, closing_markets) -> None:
        """
        Plans a decision to take for the specified time point given the household and made contracts
        :param ahead_time: the number of time steps in the future the decision should be planned for
        :param closing_markets: a list of markets for which this call of plan_decision is the last chance to place an offer
        """

        CURRENT_T = (self.time - dt.datetime.strptime(config.T_START, "%Y-%m-%d %H:%M:%S") + config.T_DELTA * ahead_time) / config.T_DELTA
        ERRORS = []

        placement_time = self.time + config.T_DELTA * ahead_time
        (load, pv, battery, charge_old, discharge_old, grid_demand_old, grid_supply_old) = self.getForecasts(ahead_time)
        prices = self.getMarketPrediction(ahead_time)
        min_surplus = self.getMinSurplus(ahead_time)

        if(CURRENT_T in ERRORS):
            print(f"Counter {CURRENT_T}, time {self.time}, plan for {placement_time}")
            print(f"Baseline: load: {load}; pv: {pv}; battery: {battery}, charge_old: {charge_old}, discharge_old: {discharge_old}; " + \
                  f"grid_demand_old: {grid_demand_old}; grid_supply_old: {grid_supply_old}, min_surplus: {min_surplus}")
            print(prices)

        # determine the best market to currently place an offer, i.e. the market with the highest price forecast where it is permissible to offer now
        best_market = "IC"
        # check intraday auction market
        if(self.time.date() < placement_time.date() and
           self.time.time() <= dt.datetime.strptime(config.INTRADAY_AUCTION_CLOSURE, "%H:%M:%S").time()):
            if(prices["IA"] > prices[best_market]):
                best_market = "IA"
        # check day-ahead market
        if(self.time.date() < placement_time.date() and
           self.time.time() <= dt.datetime.strptime(config.DAY_AHEAD_CLOSURE, "%H:%M:%S").time()):
            if(prices["DA"] > prices[best_market]):
                best_market = "DA"
        
        # obtain the best price achievable based on current price forecasts
        best_price = prices[best_market]
        if(CURRENT_T in ERRORS): print(f"Best market is {best_market} at {best_price}")

        # check whether the decision for the currently best market can be postponed
        if(not (best_market in closing_markets)):
            if(CURRENT_T in ERRORS): print("Non-closing market skipped\n")
            return

        # variables to be determined in this action
        charge = 0
        discharge = 0
        grid_demand = 0
        grid_supply = 0

        # calculate the energy surplus for the given time point, taking contracts already made for this time point into account
        surplus = pv - load + discharge_old - charge_old + grid_demand_old - grid_supply_old
        for c in self.contracts:
            (_, t, q, _) = c
            if(placement_time == t):
                surplus -= q
                if(CURRENT_T in ERRORS): print(f"Consider prior contract of {q} at {t}")

        if(CURRENT_T in ERRORS): print(f"Surplus: {surplus}")

        # first, satisfy own demand when the base load is higher than the pv generation and the demand from already made contracts
        if(surplus < 0):
            # use the battery if possible
            if(surplus + battery >= 0):
                discharge = - surplus # discharge the battery partially to satisfy base load
                if(CURRENT_T in ERRORS): print("Action: partial discharge\n")
            else:
                discharge = battery # discharge the battery fully
                grid_demand = - (battery + surplus) # fulfill the remaining demand from the grid
                if(CURRENT_T in ERRORS): print("Action: full discharge with grid demand\n")

            self.updateForecasts(ahead_time, charge, discharge, grid_demand, grid_supply, 0)
            return # in this case, no offers are placed

        # from here on, it is assumed that there is a surplus, so one now wants to make optimal use of this additional energy
        # first, check whether the minimum offer quantitiy for the energy market is satisfied
        if(surplus + battery < config.MIN_OFFER_QUANTITY or surplus + min_surplus < config.MIN_OFFER_QUANTITY):
            if(battery + surplus < config.BATTERY_CHARGE_MAX):
                charge = surplus # charge the battery with the energy surplus and place no market offers
                if(CURRENT_T in ERRORS): print("Action: charge due to insufficient quantity")
            else:
                charge = config.BATTERY_CHARGE_MAX - battery # fully charge the battery
                grid_supply = surplus - charge # feed the remaining energy into the grid
                if(CURRENT_T in ERRORS): print(f"Action: partial charge and grid supply of {grid_supply} due to insufficient quantity, charge {charge}, discharge {discharge}")
            
            if(ahead_time == 1):
                self.updateForecasts(ahead_time, charge, discharge, grid_demand, grid_supply, 0)
                if(CURRENT_T in ERRORS): print("Action executed")
            if(CURRENT_T in ERRORS): print()
            return

        # check whether the market price is lower than the grid residential price
        if(best_price < config.GRID_PRICE_RESIDENTIAL):
            # if so, use the energy to charge the battery or offer a fraction of it on the market if the battery is full
            if(battery + surplus < config.BATTERY_CHARGE_MAX): # charge the battery if possible
                charge = surplus # charge the battery with the energy surplus and place no market offers
                if(ahead_time == 1): self.updateForecasts(ahead_time, charge, discharge, grid_demand, grid_supply, 0)
                if(CURRENT_T in ERRORS): print(f"Action: charge due to small price at {best_market} of {best_price} €/kWh\n")
                return
            
            if(best_price < config.GRID_PRICE_FEEDIN):
                grid_supply = surplus
                if(ahead_time == 1): self.updateForecasts(ahead_time, charge, discharge, grid_demand, grid_supply, 0)
                if(CURRENT_T in ERRORS): print(f"Action: grid supply of {grid_supply} kWh due to small price\n")
                return

            # offer the minimum amount of energy on the market otherwise
            discharge = max(config.MIN_OFFER_QUANTITY - surplus, 0) # discharge the battery to satisfy the minimum offer quantity
            self.placeOffer(best_market, placement_time, surplus + discharge, best_price)
            self.updateForecasts(ahead_time, charge, discharge, grid_demand, grid_supply, surplus + discharge)
            if(CURRENT_T in ERRORS): print(f"Action: market offer at {best_market} of {surplus + discharge} for {best_price} €/kWh based on full battery\n")
            return

        # if none of the above restrictions failed, place a full offer on the market # TODO check for min_surplus
        discharge = min(battery, min_surplus) # if yes, discharge the battery as much as possible to offer this energy on the market
        self.placeOffer(best_market, placement_time, surplus + discharge, best_price) # construct the market offer
        self.updateForecasts(ahead_time, charge, discharge, grid_demand, grid_supply, surplus + discharge)
        if(CURRENT_T in ERRORS): print(f"Action: market offer at {best_market} of {surplus + battery} for {best_price} €/kWh based on high price\n")

    def getMarketPrediction(self, ahead_time) -> dict(): # TODO adapt this to deliver time-sensitive market price predictions
        """
        Gives a price forecast for the different markets based on past market prices and the forecast time
        :param ahead_time: the forecast time
        :return: a dictionary with the markets as keys and the price forecasts as values
        """

        index = ((dt.timedelta(hours = self.time.hour, minutes = self.time.minute) // config.T_DELTA) + ahead_time) % 96

        res = dict()
        res["DA"] = self.price_dict["DA"][index] * (1 - math.sqrt(ahead_time) * config.VOLA_DA)
        res["IA"] = self.price_dict["IA"][index] * (1 - math.sqrt(ahead_time) * config.VOLA_IA)
        res["IC"] = self.price_dict["IC"][index] * (1 - math.sqrt(ahead_time) * config.VOLA_IC)
        #print(f"Index {index}, for {self.time}, ahead_time {ahead_time}, return {res}")
        return res

    def getForecasts(self, ahead_time) -> tuple():
        """
        Gives the load, pv and battery data for the given point ahead in time
        :param ahead_time: the number of timesteps to look ahead
        :return: the load, pv and battery data for time point current_time + ahead time as a tuple in the form
        (load, pv, battery_state, battery_charge, battery_discharge, grid_demand, grid_supply)
        """

        # check if the data was already loaded for ahead_time
        while(self.valid_f <= ahead_time):
            # get the pv and load data
            pv = self.household.getPV()
            load = self.household.getLoad()
            self.pv_forecast[(self.index_f + self.valid_f) % self.length_forecast] = pv
            self.load_forecast[(self.index_f + self.valid_f) % self.length_forecast] = load

            # advance the (aggregated) battery and surplus forecast
            self.battery_forecast[(self.index_f + self.valid_f) % self.length_forecast] = \
                self.battery_forecast[(self.index_f + self.valid_f - 1) % self.length_forecast]
            self.surplus_agg[(self.index_f + self.valid_f) % self.length_forecast] = \
                self.surplus_agg[(self.index_f + self.valid_f - 1) % self.length_forecast] + pv - load
            
            # advance the non-aggregated forecasts
            self.charge[(self.index_f + self.valid_f) % self.length_forecast] = 0
            self.discharge[(self.index_f + self.valid_f) % self.length_forecast] = 0
            self.grid_demand[(self.index_f + self.valid_f) % self.length_forecast] = 0
            self.grid_supply[(self.index_f + self.valid_f) % self.length_forecast] = 0

            # advance the validity index
            self.valid_f += 1

        return (self.load_forecast[(self.index_f + ahead_time) % self.length_forecast],
                self.pv_forecast[(self.index_f + ahead_time) % self.length_forecast],
                self.battery_forecast[(self.index_f + ahead_time) % self.length_forecast],
                self.charge[(self.index_f + ahead_time) % self.length_forecast],
                self.discharge[(self.index_f + ahead_time) % self.length_forecast],
                self.grid_demand[(self.index_f + ahead_time) % self.length_forecast],
                self.grid_supply[(self.index_f + ahead_time) % self.length_forecast])
    
    def getMinSurplus(self, ahead_time) -> float:
        """
        Finds and returns the minimum future energy surplus.
        This is used to decide whether placing a contract now would compromise the fulfillment of a future contract
        :param ahead_time: the time at which to start searching for the minimum
        :return: the minimum energy surplus, starting from time point ahead_time
        """

        min = self.surplus_agg[(self.index_f + ahead_time) % self.length_forecast]

        for i in range(ahead_time, self.valid_f):
            index = (self.index_f + i) % self.length_forecast
            if(self.surplus_agg[index] < min):
                min = self.surplus_agg[index]

        return min
    
    def updateHousekeeping(self) -> None:
        """
        Updates the technical housekeeping variables for the load, pv and battery forecast
        """

        self.index_f += 1

        if(self.index_f % self.length_forecast == 0):
            self.index_f = 0

        self.valid_f -= 1

    def updateForecasts(self, ahead_time, charge, discharge, grid_demand, grid_supply, delivered) -> None:
        """
        Updates the battery state and grid forecasts, given a new charge/discharge or grid demand/supply occurring at ahead_time
        :param ahead_time: the time at which the new charge/dischrage or grid demand/supply occurs
        :param charge: charging value
        :param discharge: discharging value
        :param grid_demand: grid demand
        :param grid_supply: grid supply
        :param delivered: amount of energy delivered to the market
        """

        self.grid_demand[(self.index_f + ahead_time) % self.length_forecast] += grid_demand
        self.grid_supply[(self.index_f + ahead_time) % self.length_forecast] += grid_supply

        self.charge[(self.index_f + ahead_time) % self.length_forecast] += charge
        self.discharge[(self.index_f + ahead_time) % self.length_forecast] += discharge

        for i in range(ahead_time, self.valid_f):
            index = (self.index_f + i) % self.length_forecast
            self.battery_forecast[index] += charge - discharge
            self.surplus_agg[index] += discharge - charge + grid_demand - grid_supply - delivered

            # rebalance the battery with the aggregated surplus if the forecasted charge drops below zero
            if(self.battery_forecast[index] < 0): # this rebalancing assumes that the surplus is large enough to act as a sufficient battery recharge
                if(self.surplus_agg[index] < - self.battery_forecast[index]):
                    print("ERROR: insufficient surplus, undefined behavior may be imminent")
                self.surplus_agg[index] += self.battery_forecast[index]
                self.battery_forecast[index] = 0

    def placeOffer(self, market, del_time, quantity, bid_price) -> None:
        """
        Places an offer with the specified arguments on the market and updates the agent data accordingly
        """

        # place the contract on the market
        c = (market, del_time, quantity, bid_price)
        self.contracts.append(c)
        valid = self.market.place_offer(c) # place offer and observe its validity
        if(not valid):
            self.violations += 1
